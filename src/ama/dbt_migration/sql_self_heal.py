from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import sqlglot
from sqlglot import errors

from ama.dbt_migration.models import TargetDialect


def strip_dbt_jinja_blocks(sql: str) -> str:
    """
    dbt SQL often starts with config blocks like `{{ config(...) }}`.
    Strip them so SQLGlot can validate the underlying SELECT statement.
    """

    return re.sub(r"{{.*?}}", "", sql or "", flags=re.DOTALL).strip()


@dataclass
class SelfHealAttempt:
    validation_ok: bool
    reasons: list[str] = field(default_factory=list)
    sql_preview: str = ""
    correction_attempt: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "validation_ok": self.validation_ok,
            "reasons": list(self.reasons),
            "sql_preview": self.sql_preview,
            "correction_attempt": self.correction_attempt,
        }


ValidateSqlFn = Callable[[str, TargetDialect], tuple[bool, list[str]]]
SelfCorrectSqlFn = Callable[[str, list[str], int, TargetDialect], str]
ThoughtFn = Callable[[str, dict[str, Any]], None]


def validate_sql_with_sqlglot(sql: str, target_dialect: TargetDialect) -> tuple[bool, list[str]]:
    """
    Syntax validation only (no dbt macro semantics).
    Returns (ok, reasons).
    """
    sql_for_parse = strip_dbt_jinja_blocks(sql)
    if not sql_for_parse:
        return False, ["SQL empty after stripping dbt Jinja blocks."]
    try:
        sqlglot.parse_one(sql_for_parse, read=target_dialect.value)
    except errors.SqlglotError as exc:
        return False, [str(exc)]
    except Exception as exc:  # pragma: no cover
        return False, [f"Unexpected validation error: {exc}"]
    return True, []


def run_sql_self_healing_loop(
    *,
    initial_sql: str,
    target_dialect: TargetDialect,
    max_correction_attempts: int,
    validate_sql: ValidateSqlFn | None = None,
    self_correct_sql: SelfCorrectSqlFn,
    thought_callback: ThoughtFn | None = None,
) -> tuple[str, list[str], list[SelfHealAttempt], bool]:
    """
    Self-healing loop:
    - validate candidate SQL using QA validator
    - if rejected, call Developer self-correction to obtain next candidate
    - repeat until ok or we exhaust max_correction_attempts

    Returns:
      (final_sql, final_reasons, attempts, hitl_required)
    """

    validate_sql = validate_sql or validate_sql_with_sqlglot
    attempts: list[SelfHealAttempt] = []

    candidate = initial_sql
    ok, reasons = validate_sql(candidate, target_dialect=target_dialect)
    attempts.append(
        SelfHealAttempt(
            validation_ok=ok,
            reasons=reasons,
            sql_preview=(candidate or "")[:160],
            correction_attempt=0,
        )
    )

    if thought_callback is not None:
        thought_callback(
            "THOUGHT",
            {
                "agent_role": "QA Lead",
                "message": f"Validate SQL with sqlglot (ok={ok}).",
                "correction_attempt": 0,
                "reasons": reasons[:3],
            },
        )

    if ok:
        return candidate, [], attempts, False

    for correction_no in range(1, max_correction_attempts + 1):
        if thought_callback is not None:
            thought_callback(
                "THOUGHT",
                {
                    "agent_role": "QA Lead",
                    "message": f"SQL rejected; request Developer self-correction (attempt {correction_no}/{max_correction_attempts}).",
                    "correction_attempt": correction_no,
                    "reasons": reasons[:4],
                },
            )

        candidate = self_correct_sql(candidate, reasons, correction_no, target_dialect)

        ok, reasons = validate_sql(candidate, target_dialect=target_dialect)
        attempts.append(
            SelfHealAttempt(
                validation_ok=ok,
                reasons=reasons,
                sql_preview=(candidate or "")[:160],
                correction_attempt=correction_no,
            )
        )

        if thought_callback is not None:
            thought_callback(
                "THOUGHT",
                {
                    "agent_role": "Developer",
                    "message": f"Self-correction applied. Validate again (ok={ok}).",
                    "correction_attempt": correction_no,
                    "reasons": reasons[:3],
                },
            )

        if ok:
            return candidate, [], attempts, False

    # Exhausted corrections: request HITL.
    if thought_callback is not None:
        thought_callback(
            "THOUGHT",
            {
                "agent_role": "QA Lead",
                "message": "Self-healing exhausted. HITL required for SQL remediation.",
                "correction_attempt": max_correction_attempts,
                "reasons": reasons[:5],
            },
        )
    return candidate, reasons, attempts, True

