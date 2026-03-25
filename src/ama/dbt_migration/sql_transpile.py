from __future__ import annotations

import sqlglot
from sqlglot import errors

from ama.dbt_migration.models import TargetDialect


def validate_target_dialect(dialect: str) -> TargetDialect:
    raw = (dialect or "").strip().lower()
    try:
        parsed = TargetDialect(raw)
    except ValueError as exc:
        raise ValueError(f"Unsupported TARGET_DIALECT: {dialect}") from exc
    if raw not in sqlglot.dialects.DIALECT_MODULE_NAMES:
        raise ValueError(f"TARGET_DIALECT not supported by SQLGlot: {dialect}")
    return parsed


def transpile_sql(sql: str, target_dialect: TargetDialect) -> str:
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
        return parsed.sql(dialect=target_dialect.value, pretty=True)
    except errors.SqlglotError as exc:
        raise ValueError(f"SQL transpilation failed for {target_dialect.value}: {exc}") from exc
