"""SQLGlot AST helpers for column/table extraction (shared by sql_pipeline and lineage)."""

from __future__ import annotations

from collections import defaultdict

from sqlglot import exp

from ama.sanitize import normalize_sql_identifier


def qualified_key_from_table(node: exp.Table) -> str:
    """
    Stable database.schema.table key from sqlglot (supports multi-part names).
    Uses rendered SQL so 4-part names stay consistent with the parser.
    """
    raw = node.sql()
    s = raw.replace('"', "").replace("`", "").strip()
    if not s:
        return ""
    parts = [normalize_sql_identifier(p) for p in s.split(".") if p.strip()]
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
    alias_to_table: dict[str, str] = {}
    default_table: str | None = None
    for frm in select_expr.find_all(exp.From):
        if not frm.this:
            continue
        node = frm.this
        alias = None
        if isinstance(node, exp.Alias):
            alias = _norm_ident(node.alias)
            inner = node.this
        else:
            inner = node

        if isinstance(inner, exp.Table):
            key = qualified_key_from_table(inner)
            if not default_table:
                default_table = key
            if alias:
                alias_to_table[alias] = key
            short = _norm_ident(str(inner.name)) or str(inner.name)
            alias_to_table[short] = key
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
