"""
Optional real-extraction hooks for live schema providers.

Providers implement ``extract_ddl`` / ``extract_logs`` via duck typing — no ABC changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ama.mcp.base import SchemaProvider, TableSchema

# Regex-based best-effort literal redaction — not a full SQL parser; edge cases may slip through.
_STRING_LITERAL_RE = re.compile(
    r"(?i)(?<![\w])N?'(?:''|[^'])*'",
)
_COMPARISON_NUMERIC_RE = re.compile(
    r"([=<>])\s*-?\d+(?:\.\d+)?",
)
_IN_CLAUSE_RE = re.compile(
    r"(?i)\bIN\s*\(([^)]+)\)",
)
_SYSTEM_SQL_RES = (
    re.compile(r"\[sys\]\.", re.I),
    re.compile(r"(?<![\w])sys\.", re.I),
    re.compile(r"\binformation_schema\b", re.I),
    re.compile(r"\bdm_exec\b", re.I),
    re.compile(r"\bquery_store\b", re.I),
    re.compile(r"\bmsdb\.", re.I),
    re.compile(r"\bmaster\.", re.I),
)
_ADMIN_PREFIX_RE = re.compile(
    r"^\s*(SET|USE|DBCC|BACKUP|RESTORE|ALTER\s+DATABASE|CREATE\s+DATABASE|DROP\s+DATABASE)\b",
    re.I,
)
_GO_SPLIT_RE = re.compile(r"^\s*GO\s*(?:\r?\n|$)", re.I | re.M)


def is_noise_or_system_sql(sql: str) -> bool:
    """True for blank, admin, or system-catalog SQL unsuitable for migration discovery."""
    s = str(sql or "").strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith("sp_") or low.startswith("xp_"):
        return True
    if low.startswith("set showplan"):
        return True
    if _ADMIN_PREFIX_RE.match(s):
        return True
    return any(p.search(s) for p in _SYSTEM_SQL_RES)


def references_user_schema(sql: str, schemas: list[str]) -> bool:
    """True when SQL text references at least one requested schema (e.g. dbo.)."""
    if not schemas:
        return True
    for schema in schemas:
        sch = str(schema or "").strip()
        if not sch:
            continue
        if re.search(rf"\[{re.escape(sch)}\]\.", sql, re.I):
            return True
        if re.search(rf"(?<![\w]){re.escape(sch)}\.", sql, re.I):
            return True
    return False


def split_tsql_batch(batch: str) -> list[str]:
    """
    Split SSMS-style batches on ``GO`` lines so block comments before each statement are kept.

    Plan-cache statement offsets often drop leading ``/* … */`` markers; batch splitting preserves them.
    """
    text = str(batch or "").strip()
    if not text:
        return []
    parts = _GO_SPLIT_RE.split(text)
    out: list[str] = []
    for part in parts:
        chunk = part.strip()
        if not chunk or is_noise_or_system_sql(chunk):
            continue
        out.append(chunk)
    return out or ([text] if not is_noise_or_system_sql(text) else [])


def expand_plan_cache_sql_rows(rows: list[str]) -> list[str]:
    """Expand plan-cache batch text into per-statement chunks (preserves SSMS ``GO`` batches)."""
    expanded: list[str] = []
    for batch in rows:
        expanded.extend(split_tsql_batch(batch))
    return expanded


def filter_application_sql_texts(
    raw_sqls: list[str],
    schemas: list[str],
    max_rows: int,
) -> tuple[list[str], int]:
    """
    Keep application SQL referencing user schemas; drop system/noise batches.

    Returns (filtered_sqls, skipped_count). Dedupe is applied by the caller.
    """
    kept: list[str] = []
    skipped = 0
    for sql in raw_sqls:
        if is_noise_or_system_sql(sql):
            skipped += 1
            continue
        if schemas and not references_user_schema(sql, schemas):
            skipped += 1
            continue
        kept.append(sql.strip())
        if len(kept) >= max_rows * 20:
            break
    return kept, skipped


def redact_sql_literals(sql: str) -> str:
    """
    Best-effort redaction of embedded literal values in SQL text.

    - Single-quoted strings (including N'...' and escaped '') → '<REDACTED>'
    - Standalone numeric comparison values → <N>

    Not a full SQL parser; document limitation at call sites.
    """
    text = str(sql or "")
    if not text.strip():
        return text
    text = _STRING_LITERAL_RE.sub("'<REDACTED>'", text)
    text = _COMPARISON_NUMERIC_RE.sub(r"\1 <N>", text)
    text = _IN_CLAUSE_RE.sub(
        lambda m: "IN (" + re.sub(r"\b-?\d+(?:\.\d+)?\b", "<N>", m.group(1)) + ")",
        text,
    )
    return text


@dataclass
class LogExtractionResult:
    """Outcome of provider.extract_logs()."""

    records: list[dict[str, str]]  # {"env", "dialect", "sql"}
    source: str  # "query_store" | "plan_cache"
    date_range_applied: bool
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int | str] = field(default_factory=dict)


def supports_real_extraction(provider: SchemaProvider) -> bool:
    """True when provider implements both extract hooks."""
    return hasattr(provider, "extract_ddl") and hasattr(provider, "extract_logs")


def normalize_sql_for_dedupe(sql: str) -> str:
    """Collapse whitespace for dedupe keys (pre-redaction)."""
    return " ".join(str(sql or "").split()).lower()


def ddl_filename(schema: str, table: str) -> str:
    """Stable artifact name, e.g. dbo_orders.json."""
    return f"{schema.lower()}_{table.lower()}.json"


def table_schema_to_ddl_json(ts: TableSchema) -> dict[str, object]:
    """Format compatible with load_ddl_columns(); optional PK/FK metadata for schema lineage."""
    out: dict[str, object] = {"columns": [c.name for c in ts.columns]}
    pks = [c.name for c in ts.columns if c.primary_key]
    if pks:
        out["primary_keys"] = pks
    fks: list[dict[str, str]] = []
    for c in ts.columns:
        ref = str(c.foreign_key_ref or "").strip()
        if not ref:
            continue
        parts = [p for p in ref.split(".") if p]
        if len(parts) < 3:
            continue
        fks.append(
            {
                "column": c.name,
                "references_table": ".".join(parts[:2]),
                "references_column": parts[2],
            }
        )
    if fks:
        out["foreign_keys"] = fks
    return out
