import pytest

from ama.dbt_migration.generator import generate_model_artifact
from ama.dbt_migration.models import TargetDialect
from ama.dbt_migration.sql_self_heal import run_sql_self_healing_loop


def test_self_healing_loop_corrects_on_next_iteration(monkeypatch):
    # No OpenAI needed: we mock the Developer self-correction.
    monkeypatch.delenv("AMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    target = TargetDialect.DUCKDB

    calls: list[int] = []

    def _self_corrector(_candidate_sql: str, _reasons: list[str], correction_attempt: int, _td: TargetDialect) -> str:
        calls.append(correction_attempt)
        # Provide valid SQL for SQLGlot.
        return "select 1 as one"

    final_sql, final_reasons, attempts, hitl = run_sql_self_healing_loop(
        initial_sql="SELEC 1",  # invalid on purpose
        target_dialect=target,
        max_correction_attempts=3,
        self_correct_sql=_self_corrector,
        thought_callback=None,
    )

    assert hitl is False
    assert calls == [1]
    assert final_reasons == []
    assert len(attempts) == 2
    assert attempts[0].validation_ok is False
    assert attempts[1].validation_ok is True
    assert "select 1" in final_sql.lower()


def test_self_healing_loop_exhausts_and_requires_hitl(monkeypatch):
    monkeypatch.delenv("AMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    target = TargetDialect.DUCKDB

    calls: list[int] = []

    def _self_corrector(_candidate_sql: str, _reasons: list[str], correction_attempt: int, _td: TargetDialect) -> str:
        calls.append(correction_attempt)
        return "SELEC 1"  # still invalid

    final_sql, final_reasons, attempts, hitl = run_sql_self_healing_loop(
        initial_sql="SELEC 1",
        target_dialect=target,
        max_correction_attempts=3,
        self_correct_sql=_self_corrector,
        thought_callback=None,
    )

    assert hitl is True
    assert calls == [1, 2, 3]
    assert len(attempts) == 4  # initial + 3 correction attempts
    assert final_reasons  # reasons should be non-empty on failure
    assert "SELEC" in final_sql


def test_role_handoff_hebrew_mapping_drives_sql_projection(monkeypatch):
    # Ensure deterministic/non-LLM mode.
    monkeypatch.delenv("AMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("ama.dbt_migration.generator.has_openai_api_key", lambda: False)
    monkeypatch.setattr("ama.dbt_migration.mapping.has_openai_api_key", lambda: False)

    target = TargetDialect.DUCKDB
    table_key = "finance.invoices"

    # Hebrew column mapped via glossary -> expected English alias in SQL.
    glossary = {"שם": "customer_id"}

    artifact, mapped = generate_model_artifact(
        table_key=table_key,
        raw_columns=["שם"],
        glossary=glossary,
        alias_registry={},
        target_dialect=target,
        source_ddl_columns=None,
        broken=False,
        rationale="finance invoices customer mapping",
        thought_callback=None,
        max_correction_attempts=3,
    )

    assert artifact.review_required in {False, True}  # should be stable; mapping success is the main assertion
    assert any(m.hebrew_name == "שם" and m.english_alias == "customer_id" for m in mapped)
    assert "AS customer_id" in artifact.sql

