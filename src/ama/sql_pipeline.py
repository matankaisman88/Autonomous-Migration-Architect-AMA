from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ama.sanitize import normalize_sql_identifier, sanitize_sql_text


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


@dataclass
class ColumnStats:
    select: int = 0
    where: int = 0
    join_on: int = 0
    group_by: int = 0
    order_by: int = 0


@dataclass
class TableColumnStats:
    columns: dict[str, ColumnStats] = field(default_factory=lambda: defaultdict(ColumnStats))
    query_count: int = 0


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


def _extract_from_select(sel: exp.Select) -> dict[str, dict[str, int]]:
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


def _table_matches_target(tl: str, target_keys: set[str], short: str) -> bool:
    tl_norm = normalize_sql_identifier(tl)
    tl_cmp = tl_norm.lower() if all(ord(c) < 128 for c in tl_norm) else tl_norm
    short_cmp = short.lower() if short else short
    if tl_cmp in target_keys:
        return True
    if tl_cmp == short_cmp:
        return True
    return tl_cmp.endswith("." + short_cmp)


def _merge_role_counts_into_stats(
    flat: dict[str, dict[str, int]],
    target_keys: set[str],
    short: str,
    stats: TableColumnStats,
) -> None:
    for tbl, role_cols in flat.items():
        tl = tbl
        if not _table_matches_target(tl, target_keys, short):
            continue
        for rk, n in role_cols.items():
            role, _, col = rk.partition(":")
            if not col or col == "*":
                continue
            cs = stats.columns[col]
            if role == "select":
                cs.select += n
            elif role == "where":
                cs.where += n
            elif role == "join_on":
                cs.join_on += n
            elif role == "group_by":
                cs.group_by += n
            elif role == "order_by":
                cs.order_by += n


def parse_sql_query(sql_text: str, dialect: str | None = None) -> tuple[list[dict[str, dict[str, int]]], bool]:
    text = sanitize_sql_text(sql_text)
    if not text or text.startswith("--"):
        return [], False
    try:
        parsed = sqlglot.parse_one(text, dialect=dialect)
    except ParseError:
        return [], False

    chunks: list[dict[str, dict[str, int]]] = []
    for sel in parsed.find_all(exp.Select):
        chunks.append(_extract_from_select(sel))
    if chunks:
        return chunks, True

    # UNION etc. — try full tree
    if isinstance(parsed, exp.Select):
        return [_extract_from_select(parsed)], True
    return [], False


def _fallback_regex_extract(text: str) -> list[dict[str, dict[str, int]]]:
    per_table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    t = sanitize_sql_text(text)
    for m in re.finditer(
        r"\bfrom\s+([\w]+)\s*\.\s*([\w]+)",
        t,
        re.IGNORECASE,
    ):
        a = normalize_sql_identifier(m.group(1))
        b = normalize_sql_identifier(m.group(2))
        key = f"{a}.{b}" if a and b else ""
        if key:
            per_table[key]["select:*"] += 1
    for m in re.finditer(r"\bfrom\s+([\w]+)\b", t, re.IGNORECASE):
        w = normalize_sql_identifier(m.group(1))
        if w and w not in ("select", "lateral", "unnest"):
            per_table[w]["select:*"] += 1
    return [dict(per_table)] if per_table else []


def iter_sql_log_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.replace("\x00", "").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def table_matches_target(table_key: str, target_full_table: str) -> bool:
    """True if a qualified table key refers to the same object as target_full_table (schema.table)."""
    tkeys = _target_keys(target_full_table)
    short = normalize_sql_identifier(target_full_table.split(".")[-1])
    return _table_matches_target(table_key, tkeys, short)


def _target_keys(target_full_table: str) -> set[str]:
    schema, _, table = target_full_table.partition(".")
    t = normalize_sql_identifier(table or target_full_table)
    s = normalize_sql_identifier(schema) if schema else ""
    keys: set[str] = {t, normalize_sql_identifier(target_full_table)}
    if s:
        keys.add(f"{s}.{t}")
    return keys


def process_sql_log_file(
    path: Path,
    *,
    target_full_table: str,
    env: str | None = "prod",
) -> TableColumnStats:
    out = TableColumnStats()
    tkeys = _target_keys(target_full_table)
    short = normalize_sql_identifier(target_full_table.split(".")[-1])

    for rec in iter_sql_log_records(path):
        if env and rec.get("env") and str(rec["env"]).lower() != str(env).lower():
            continue
        sql = rec.get("sql") or rec.get("query") or rec.get("statement")
        if not sql or not isinstance(sql, str):
            continue
        sql = sanitize_sql_text(sql)
        dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else None
        chunks, ok = parse_sql_query(sql, dialect=dialect)
        if not ok:
            chunks = _fallback_regex_extract(sql)
            if not chunks:
                continue

        matched = False
        for flat in chunks:
            for tbl in flat:
                if _table_matches_target(tbl, tkeys, short):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            continue

        out.query_count += 1
        for flat in chunks:
            _merge_role_counts_into_stats(flat, tkeys, short, out)

    return out


def merge_stats(into: TableColumnStats, other: TableColumnStats) -> None:
    into.query_count += other.query_count
    for col, oc in other.columns.items():
        ic = into.columns[col]
        ic.select += oc.select
        ic.where += oc.where
        ic.join_on += oc.join_on
        ic.group_by += oc.group_by
        ic.order_by += oc.order_by


def merge_flat_into_table_stats(
    flat: dict[str, dict[str, int]],
    table_key: str,
    stats: TableColumnStats,
) -> None:
    """Merge role counts for one table from a flat chunk into stats (discovery / multi-table)."""
    role_cols = flat.get(table_key)
    if not role_cols:
        return
    for rk, n in role_cols.items():
        role, _, col = rk.partition(":")
        if not col or col == "*":
            continue
        cs = stats.columns[col]
        if role == "select":
            cs.select += n
        elif role == "where":
            cs.where += n
        elif role == "join_on":
            cs.join_on += n
        elif role == "group_by":
            cs.group_by += n
        elif role == "order_by":
            cs.order_by += n


def process_sql_log_file_discovery(
    path: Path,
    *,
    env: str | None = "prod",
) -> dict[str, TableColumnStats]:
    """Aggregate column stats per qualified table key (no target filter)."""
    per_table: dict[str, TableColumnStats] = defaultdict(TableColumnStats)

    for rec in iter_sql_log_records(path):
        if env and rec.get("env") and str(rec["env"]).lower() != str(env).lower():
            continue
        sql = rec.get("sql") or rec.get("query") or rec.get("statement")
        if not sql or not isinstance(sql, str):
            continue
        sql = sanitize_sql_text(sql)
        dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else None
        chunks, ok = parse_sql_query(sql, dialect=dialect)
        if not ok:
            chunks = _fallback_regex_extract(sql)
            if not chunks:
                continue

        touched: set[str] = set()
        for flat in chunks:
            for tbl in flat:
                touched.add(tbl)
        for tbl in touched:
            per_table[tbl].query_count += 1

        for flat in chunks:
            for tbl in flat:
                merge_flat_into_table_stats(flat, tbl, per_table[tbl])

    return dict(per_table)


def run_sql_logs_discovery_pipeline(
    sql_log_paths: list[Path],
    *,
    env: str | None = "prod",
) -> dict[str, TableColumnStats]:
    total: dict[str, TableColumnStats] = defaultdict(TableColumnStats)
    for p in sql_log_paths:
        part = process_sql_log_file_discovery(p, env=env)
        for k, st in part.items():
            merge_stats(total[k], st)
    return dict(total)


def run_sql_logs_pipeline(
    sql_log_paths: list[Path],
    *,
    target_full_table: str,
    env: str | None = "prod",
) -> TableColumnStats:
    total = TableColumnStats()
    for p in sql_log_paths:
        other = process_sql_log_file(p, target_full_table=target_full_table, env=env)
        merge_stats(total, other)
    return total
