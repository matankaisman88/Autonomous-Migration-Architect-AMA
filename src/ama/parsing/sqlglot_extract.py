"""SQLGlot AST helpers for column/table extraction (shared by sql_pipeline and lineage)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ama.sanitize import normalize_sql_identifier


def qualified_key_from_table(node: exp.Table) -> str:
    """
    Stable schema.table key from sqlglot AST parts — never includes alias.

    Uses node.parts (catalog, db, name as Identifier objects) rather than
    node.sql() so aliased references like ``FROM finance.payments AS p``
    yield ``finance.payments``, not ``finance.payments_as_p``.
    """
    parts = [normalize_sql_identifier(p.name) for p in node.parts if p.name]
    parts = [p for p in parts if p]
    return ".".join(parts) if parts else ""


def _norm_ident(name: str | None) -> str | None:
    if name is None:
        return None
    out = normalize_sql_identifier(str(name))
    return out if out else None


def _table_key(schema: str | None, table: str) -> str:
    s = _norm_ident(schema) or ""
    t = _norm_ident(table) or table
    if s:
        return f"{s}.{t}"
    return t


def _collect_columns(
    node: exp.Expression | None,
    role: str,
    alias_to_table: dict[str, str],
    default_table: str | None,
    acc: dict[str, dict[str, int]],
) -> None:
    if node is None:
        return

    if isinstance(node, exp.Column):
        parts = [p.name for p in node.parts if p.name]
        if not parts:
            return
        col = _norm_ident(parts[-1]) or parts[-1]
        if len(parts) >= 2:
            tbl_hint = _norm_ident(parts[-2]) or parts[-2]
            resolved = alias_to_table.get(tbl_hint, tbl_hint)
            if "." not in resolved and default_table:
                resolved = default_table
            key = resolved if "." in str(resolved) else _table_key(None, str(resolved))
        else:
            key = default_table or ""
        if not key:
            return
        bucket = acc.setdefault(key, defaultdict(int))
        bucket[f"{role}:{col}"] += 1
        return

    if isinstance(node, exp.Star):
        return

    for child in node.iter_expressions():
        _collect_columns(child, role, alias_to_table, default_table, acc)


def _resolve_aliases(select_expr: exp.Select) -> tuple[dict[str, str], str | None]:
    """Map table aliases and short names to qualified keys (includes JOIN tables)."""
    alias_to_table: dict[str, str] = {}
    default_table: str | None = None

    def _register_from_node(node: exp.Expression | None) -> None:
        nonlocal default_table
        if not node:
            return
        alias: str | None = None
        if isinstance(node, exp.Alias):
            alias = _norm_ident(node.alias)
            inner = node.this
        else:
            inner = node
        if isinstance(inner, exp.Table):
            if not alias and inner.alias:
                al = inner.alias
                if isinstance(al, str):
                    alias = _norm_ident(al)
                elif isinstance(al, exp.TableAlias):
                    tid = al.this
                    alias = _norm_ident(
                        str(tid.name) if hasattr(tid, "name") else str(tid)
                    )
                else:
                    alias = _norm_ident(str(al))
            key = qualified_key_from_table(inner)
            if not default_table:
                default_table = key
            if alias:
                alias_to_table[alias] = key
            short = _norm_ident(str(inner.name)) or str(inner.name)
            if short:
                alias_to_table[short] = key

    for frm in select_expr.find_all(exp.From):
        _register_from_node(frm.this)
    for join in select_expr.find_all(exp.Join):
        _register_from_node(join.this)

    return alias_to_table, default_table


def extract_from_select(sel: exp.Select) -> dict[str, dict[str, int]]:
    """Returns table_key -> {role:col -> count} flattened for one SELECT."""
    alias_map, default_table = _resolve_aliases(sel)
    acc: dict[str, dict[str, int]] = {}

    for proj in sel.expressions:
        _collect_columns(proj, "select", alias_map, default_table, acc)

    if sel.args.get("where"):
        _collect_columns(sel.args["where"], "where", alias_map, default_table, acc)

    for g in sel.find_all(exp.Group):
        _collect_columns(g, "group_by", alias_map, default_table, acc)

    for o in sel.find_all(exp.Order):
        _collect_columns(o, "order_by", alias_map, default_table, acc)

    for join in sel.find_all(exp.Join):
        if join.args.get("on"):
            _collect_columns(join.args["on"], "join_on", alias_map, default_table, acc)

    return acc


@dataclass(frozen=True)
class DdlTableDetails:
    """Dialect-agnostic projection of source DDL table metadata."""

    table_key: str
    database: str | None = None
    schema: str | None = None
    table: str | None = None
    owner: str | None = None
    tablespace: str | None = None
    source_dialect: str | None = None


def _norm_sql_name(name: str | None) -> str | None:
    if not name:
        return None
    out = normalize_sql_identifier(name)
    return out or None


def _ddl_key_from_ident(schema: str | None, table: str | None) -> str:
    s = _norm_sql_name(schema)
    t = _norm_sql_name(table)
    if s and t:
        return f"{s}.{t}"
    return t or ""


def extract_ddl_table_details(ddl_sql: str, *, dialect: str | None = None) -> list[DdlTableDetails]:
    """
    Extract CREATE TABLE metadata from DDL SQL text.

    Includes dialect-specific enrichment:
    - Oracle: `owner` and `TABLESPACE`
    - DB2: schema + `IN <tablespace>` clauses
    """
    text = str(ddl_sql or "").strip()
    if not text:
        return []

    out: list[DdlTableDetails] = []
    try:
        expressions = sqlglot.parse(text, read=dialect) if dialect else sqlglot.parse(text)
    except Exception:
        expressions = []

    for ex in expressions:
        if not isinstance(ex, exp.Create):
            continue
        this = ex.this
        if not isinstance(this, exp.Table):
            continue
        parts = [p.name for p in this.parts if getattr(p, "name", None)]
        schema = parts[-2] if len(parts) >= 2 else None
        table = parts[-1] if parts else None
        database = parts[-3] if len(parts) >= 3 else None
        owner = schema
        tablespace = None

        # Oracle/DB2: sqlglot does not consistently expose all physical options;
        # read from SQL text as a reliable fallback for operational metadata.
        sql_body = ex.sql(dialect=dialect) if dialect else ex.sql()
        m_ts = re.search(r"\bTABLESPACE\s+([A-Za-z0-9_$.]+)", sql_body, flags=re.IGNORECASE)
        if m_ts:
            tablespace = m_ts.group(1)
        else:
            m_db2 = re.search(r"\bIN\s+([A-Za-z0-9_$.]+)", sql_body, flags=re.IGNORECASE)
            if m_db2:
                tablespace = m_db2.group(1)

        key = _ddl_key_from_ident(schema, table)
        if key:
            out.append(
                DdlTableDetails(
                    table_key=key,
                    database=_norm_sql_name(database),
                    schema=_norm_sql_name(schema),
                    table=_norm_sql_name(table),
                    owner=_norm_sql_name(owner),
                    tablespace=_norm_sql_name(tablespace),
                    source_dialect=(dialect or "").lower() or None,
                )
            )

    # Regex fallback for edge dialect DDL not parsed by sqlglot.
    if out:
        return out
    rgx = re.compile(
        r"create\s+table\s+([A-Za-z0-9_$.]+)\s*\(",
        flags=re.IGNORECASE,
    )
    for m in rgx.finditer(text):
        name = m.group(1)
        parts = [p for p in name.split(".") if p]
        schema = parts[-2] if len(parts) >= 2 else None
        table = parts[-1] if parts else None
        key = _ddl_key_from_ident(schema, table)
        if not key:
            continue
        after = text[m.end() : m.end() + 500]
        m_ts = re.search(r"\bTABLESPACE\s+([A-Za-z0-9_$.]+)", after, flags=re.IGNORECASE)
        if not m_ts:
            m_ts = re.search(r"\bIN\s+([A-Za-z0-9_$.]+)", after, flags=re.IGNORECASE)
        out.append(
            DdlTableDetails(
                table_key=key,
                schema=_norm_sql_name(schema),
                table=_norm_sql_name(table),
                owner=_norm_sql_name(schema),
                tablespace=_norm_sql_name(m_ts.group(1)) if m_ts else None,
                source_dialect=(dialect or "").lower() or None,
            )
        )
    return out
