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
