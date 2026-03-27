"""
Parse backend: SQLGlot + regex fallback. Centralizes dialect handling for Snowflake/BigQuery/etc.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ama.parsing.sqlglot_extract import extract_from_select
from ama.sanitize import normalize_sql_identifier, sanitize_sql_text

# Map common aliases to sqlglot dialect names (extend as needed).
DIALECT_ALIASES: dict[str, str] = {
    "bq": "bigquery",
    "bigquery": "bigquery",
    "sf": "snowflake",
    "snowflake": "snowflake",
    "pg": "postgres",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlserver": "tsql",
    "sql_server": "tsql",
    "mssql": "tsql",
    "tsql": "tsql",
    "oracle": "oracle",
    "db2": "db2",
    "spark": "spark",
    "databricks": "databricks",
}

ParseMode = Literal["sqlglot", "regex", "empty", "skipped_empty"]


def normalize_dialect(dialect: str | None) -> str | None:
    if not dialect or not isinstance(dialect, str):
        return None
    s = dialect.strip().lower()
    return DIALECT_ALIASES.get(s, s)


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing one SQL string for column/table extraction."""

    chunks: list[dict[str, dict[str, int]]]
    mode: ParseMode
    # Set only for successful sqlglot parses; consumed immediately for lineage (not retained across records).
    expression: Any | None = None


def _fallback_regex_extract(text: str) -> list[dict[str, dict[str, int]]]:
    per_table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    t = sanitize_sql_text(text)
    if not t:
        return []

    def _n(name: str | None) -> str:
        if not name:
            return ""
        return normalize_sql_identifier(name)

    def _table_key(raw_ref: str) -> str:
        # Keep stable schema.table keys; collapse 3-part refs to the rightmost 2 segments.
        parts = [_n(p) for p in re.split(r"\s*\.\s*", raw_ref or "") if _n(p)]
        if not parts:
            return ""
        if len(parts) >= 2:
            return f"{parts[-2]}.{parts[-1]}"
        return parts[0]

    alias_to_table: dict[str, str] = {}
    default_table: str | None = None
    table_pat = re.compile(
        r"\b(from|join)\s+([A-Za-z_][\w$]*(?:\s*\.\s*[A-Za-z_][\w$]*){0,2})"
        r"(?:\s+(?:as\s+)?([A-Za-z_][\w$]*))?",
        re.IGNORECASE,
    )
    for m in table_pat.finditer(t):
        raw_tbl = m.group(2)
        key = _table_key(raw_tbl)
        if not key:
            continue
        # Touch table so discovery query_count can still reflect workload in regex mode.
        _ = per_table[key]
        if default_table is None and m.group(1).lower() == "from":
            default_table = key
        alias = _n(m.group(3))
        if alias:
            alias_to_table[alias] = key
        short = key.split(".")[-1]
        if short:
            alias_to_table[short] = key

    def _add_col(role: str, col: str, tbl_hint: str | None = None) -> None:
        c = _n(col)
        if not c:
            return
        if tbl_hint:
            hint = _n(tbl_hint)
            table_key = alias_to_table.get(hint) or (default_table or "")
        else:
            table_key = default_table or ""
        if not table_key:
            return
        per_table[table_key][f"{role}:{c}"] += 1

    # 3-part and 2-part qualified identifiers: alias.col or schema.table.col.
    dotted_ident = re.compile(
        r"\b([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)(?:\s*\.\s*([A-Za-z_][\w$]*))?\b"
    )

    # Extract SELECT list and capture unqualified identifiers (e.g. c_0_1).
    m_sel = re.search(r"\bselect\b(.*?)\bfrom\b", t, re.IGNORECASE | re.DOTALL)
    if m_sel:
        sel_chunk = m_sel.group(1)
        for qm in dotted_ident.finditer(sel_chunk):
            a, b, c = qm.group(1), qm.group(2), qm.group(3)
            if c:
                _add_col("select", c, b)
            else:
                _add_col("select", b, a)
        for m in re.finditer(r"\b([A-Za-z_][\w$]*)\b", sel_chunk):
            tok = _n(m.group(1))
            if not tok:
                continue
            if tok in {
                "select",
                "from",
                "as",
                "distinct",
                "max",
                "min",
                "sum",
                "avg",
                "count",
                "case",
                "when",
                "then",
                "else",
                "end",
                "null",
                "true",
                "false",
            }:
                continue
            if tok in alias_to_table:
                continue
            # Skip function names in "fn(...)" forms.
            tail = sel_chunk[m.end() :]
            if tail.lstrip().startswith("("):
                continue
            _add_col("select", tok)

    m_where = re.search(
        r"\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|$)",
        t,
        re.IGNORECASE | re.DOTALL,
    )
    if m_where:
        where_chunk = m_where.group(1)
        for m in dotted_ident.finditer(where_chunk):
            a, b, c = m.group(1), m.group(2), m.group(3)
            if c:
                _add_col("where", c, b)
            else:
                _add_col("where", b, a)

    for m in re.finditer(r"\bjoin\b.*?\bon\b(.*?)(?=\bjoin\b|\bwhere\b|$)", t, re.IGNORECASE | re.DOTALL):
        on_chunk = m.group(1)
        for cm in dotted_ident.finditer(on_chunk):
            a, b, c = cm.group(1), cm.group(2), cm.group(3)
            if c:
                _add_col("join_on", c, b)
            else:
                _add_col("join_on", b, a)

    m_insert = re.search(
        r"\binsert\s+into\s+([A-Za-z_][\w$]*(?:\s*\.\s*[A-Za-z_][\w$]*){0,2})\s*\((.*?)\)",
        t,
        re.IGNORECASE | re.DOTALL,
    )
    if m_insert:
        ins_tbl = _table_key(m_insert.group(1))
        if ins_tbl:
            _ = per_table[ins_tbl]
            for raw_col in m_insert.group(2).split(","):
                c = _n(raw_col)
                if c:
                    per_table[ins_tbl][f"select:{c}"] += 1

    return [dict(per_table)] if per_table else []


class ParseBackend(Protocol):
    def parse(self, sql_text: str, dialect: str | None = None) -> ParseResult: ...


class SqlGlotParseBackend:
    """Default backend: SQLGlot AST walk + regex fallback (matches legacy sql_pipeline behavior)."""

    def parse(self, sql_text: str, dialect: str | None = None) -> ParseResult:
        text = sanitize_sql_text(sql_text)
        if not text or text.startswith("--"):
            return ParseResult(chunks=[], mode="skipped_empty")

        parse_mode = str(os.environ.get("AMA_SQL_PARSE_MODE") or "").strip().lower()
        if parse_mode == "regex":
            chunks = _fallback_regex_extract(text)
            return ParseResult(chunks=chunks, mode="regex" if chunks else "empty")

        d = normalize_dialect(dialect)
        try:
            parsed = sqlglot.parse_one(text, dialect=d)
        except ParseError:
            chunks = _fallback_regex_extract(text)
            return ParseResult(chunks=chunks, mode="regex" if chunks else "empty")

        chunks: list[dict[str, dict[str, int]]] = []
        for sel in parsed.find_all(exp.Select):
            chunks.append(extract_from_select(sel))
        if chunks:
            return ParseResult(chunks=chunks, mode="sqlglot", expression=parsed)

        if isinstance(parsed, exp.Select):
            return ParseResult(
                chunks=[extract_from_select(parsed)],
                mode="sqlglot",
                expression=parsed,
            )

        chunks = _fallback_regex_extract(text)
        return ParseResult(
            chunks=chunks,
            mode="regex" if chunks else "empty",
            expression=None,
        )


_default_backend: SqlGlotParseBackend | None = None


def default_parse_backend() -> SqlGlotParseBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = SqlGlotParseBackend()
    return _default_backend
