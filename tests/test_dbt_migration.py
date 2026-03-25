from __future__ import annotations

import csv
import json
from pathlib import Path

from ama.ai_query_helper import AIQueryResult, OpenAIRateLimitError
from ama.dbt_migration.generator import (
    generate_model_artifact,
    generate_models_from_manifest,
)
from ama.dbt_migration.models import (
    CheckpointAArtifact,
    MappingSource,
    MigrationSessionState,
    ModelArtifact,
    RunnerFinalStatus,
    TargetDialect,
)
from ama.dbt_migration.runner import (
    approve_checkpoint_b_sql,
    execute_models_with_fix_loop,
    reject_checkpoint_b_to_dlq,
    run_dbt_with_fix_loop,
)
from ama.dbt_migration.service import _orchestrate_waves_with_gating
from ama.dbt_migration.service import apply_ai_fix_from_checkpoint
from ama.dbt_migration.service import (
    analyze_model_risk_and_scenarios,
    generate_synthetic_data_for_model,
    propose_sql_patch_from_chat,
    run_wave_stress_test,
)
from ama.dbt_migration import service as dbt_service
from ama.dbt_migration.sql_transpile import validate_target_dialect
from ama.dbt_migration.cockpit_agents import data_gen_agent


def test_validate_target_dialect_accepts_duckdb() -> None:
    assert validate_target_dialect("duckdb") == TargetDialect.DUCKDB


def test_validate_target_dialect_rejects_unknown() -> None:
    try:
        validate_target_dialect("oracle")
    except ValueError as exc:
        assert "Unsupported TARGET_DIALECT" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def _write_manifest(path: Path, *, materialized: str = "view", unique_key=None) -> None:
    manifest = {
        "nodes": {
            "model.demo.orders_model": {
                "resource_type": "model",
                "name": "orders_model",
                "schema": "dbo",
                "alias": "orders",
                "original_file_path": "models/orders_model.sql",
                "config": {"materialized": materialized, "unique_key": unique_key},
                "columns": {
                    "id": {"name": "id"},
                    "amount": {"name": "amount"},
                },
            }
        }
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_usage(path: Path, rows: list[tuple[str, str, int]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["model_name", "column_name", "usage_count"])
        writer.writeheader()
        for model_name, column_name, usage_count in rows:
            writer.writerow(
                {
                    "model_name": model_name,
                    "column_name": column_name,
                    "usage_count": str(usage_count),
                }
            )


def test_generate_model_artifact_broken_lineage_creates_stub() -> None:
    artifact, mapped = generate_model_artifact(
        table_key="ghost.sales",
        raw_columns=["סכום"],
        glossary={},
        alias_registry={},
        target_dialect=TargetDialect.DUCKDB,
        broken=True,
        rationale="missing ddl",
    )
    assert artifact.is_stub is True
    assert "-- WARNING: UNRESOLVED BROKEN LINEAGE" in artifact.sql
    assert mapped


def test_generate_model_artifact_sets_mapping_rows_on_artifact(monkeypatch) -> None:
    monkeypatch.setattr("ama.dbt_migration.generator.has_openai_api_key", lambda: False)
    monkeypatch.setattr("ama.dbt_migration.mapping.has_openai_api_key", lambda: False)
    artifact, mapped = generate_model_artifact(
        table_key="dbo.orders",
        raw_columns=["סכום_כולל", "תאור"],
        glossary={},
        alias_registry={},
        target_dialect=TargetDialect.DUCKDB,
        broken=False,
        rationale="ok",
    )
    assert artifact.mapping_rows
    assert len(artifact.mapping_rows) == len(mapped)
    assert any("[TRANSLITERATION_WARNING]" in r.warning_flags for r in artifact.mapping_rows)
    assert artifact.generation_mode in {"ai", "legacy"}
    assert isinstance(artifact.ai_telemetry, list)


def test_generate_model_artifact_rejects_llm_row_filters(monkeypatch) -> None:
    monkeypatch.setattr("ama.dbt_migration.mapping.has_openai_api_key", lambda: False)
    monkeypatch.setattr("ama.dbt_migration.generator.has_openai_api_key", lambda: True)
    monkeypatch.setattr(
        "ama.dbt_migration.generator._call_schema_agent",
        lambda **_kwargs: ({"context_analysis": "ok", "suggested_columns": ["invoice_id"], "confidence": 0.95}, 10, 0.95),
    )
    monkeypatch.setattr(
        "ama.dbt_migration.generator._call_dbt_agent",
        lambda **_kwargs: (
            "SELECT invoice_id, status FROM finance.invoices WHERE status = 'unpaid'",
            "drafted with filter",
            20,
            0.9,
        ),
    )

    artifact, _mapped = generate_model_artifact(
        table_key="finance.invoices",
        raw_columns=["invoice_id", "status"],
        glossary={},
        alias_registry={},
        target_dialect=TargetDialect.DUCKDB,
        broken=False,
        rationale="migrate all invoices",
    )

    assert artifact.generation_mode == "legacy"
    assert "where status = 'unpaid'" not in artifact.sql.lower()


def test_generator_ignores_usage_columns_missing_from_ddl(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    usage = tmp_path / "usage.csv"
    out_dir = tmp_path / "models"
    _write_manifest(manifest)
    _write_usage(
        usage,
        [
            ("orders_model", "id", 10),
            ("orders_model", "ghost_col", 7),
        ],
    )
    models = generate_models_from_manifest(
        manifest_path=manifest,
        usage_csv_path=usage,
        output_dir=out_dir,
        target_dialect=TargetDialect.DUCKDB,
    )
    assert len(models) == 1
    sql_text = (out_dir / "orders_model.sql").read_text(encoding="utf-8").lower()
    assert "ghost_col" not in sql_text
    assert "id" in sql_text and "amount" in sql_text


def test_generator_incremental_requires_unique_key(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    usage = tmp_path / "usage.csv"
    out_dir = tmp_path / "models"
    _write_manifest(manifest, materialized="incremental", unique_key="")
    _write_usage(usage, [("orders_model", "id", 1)])
    try:
        generate_models_from_manifest(
            manifest_path=manifest,
            usage_csv_path=usage,
            output_dir=out_dir,
            target_dialect=TargetDialect.DUCKDB,
        )
    except ValueError as exc:
        assert "unique_key" in str(exc)
    else:
        raise AssertionError("expected unique_key validation failure")


def test_runner_happy_path_success(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "orders_model.sql").write_text("select 1 as id", encoding="utf-8")

    def _ok(*_args, **_kwargs):
        return 0, "ok", ""

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _ok)
    result = execute_models_with_fix_loop(
        dbt_project_dir=tmp_path,
        model_names=["orders_model"],
        max_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
    )
    assert result.status == RunnerFinalStatus.SUCCESS
    assert result.model_results[0].fix_loop_count == 0


def test_runner_fix_path_succeeds_on_second_attempt(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "orders_model.sql").write_text("bad sql", encoding="utf-8")
    calls = {"n": 0}
    patch_calls = {"n": 0}

    def _mixed(command, _cwd):
        calls["n"] += 1
        if command[:2] == ["dbt", "run"] and calls["n"] == 1:
            return 1, "", "syntax error near from"
        return 0, "ok", ""

    def _patch(_model_name: str, _error_context: str, _attempt_number: int) -> None:
        patch_calls["n"] += 1

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _mixed)
    result = execute_models_with_fix_loop(
        dbt_project_dir=tmp_path,
        model_names=["orders_model"],
        max_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
        patch_callback=_patch,
    )
    assert result.status == RunnerFinalStatus.SUCCESS
    assert patch_calls["n"] == 1
    assert result.model_results[0].fix_loop_count == 1


def test_runner_max_retry_creates_checkpoint_b(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "orders_model.sql").write_text("bad sql", encoding="utf-8")

    def _fail(*_args, **_kwargs):
        return 1, "", "column does not exist"

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _fail)
    result = execute_models_with_fix_loop(
        dbt_project_dir=tmp_path,
        model_names=["orders_model"],
        max_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
    )
    assert result.status == RunnerFinalStatus.FAILURE
    assert result.model_results[0].state.value == "HITL_REQUIRED"
    checkpoint_file = tmp_path / "checkpoints" / "checkpoint_b_orders_model.json"
    assert checkpoint_file.is_file()
    checkpoint_payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    assert "fix_agent_error_analysis" in checkpoint_payload
    assert "suggested_sql" in checkpoint_payload
    assert "tokens_used" in checkpoint_payload
    dlq_file = tmp_path / "dlq" / "dlq_records.jsonl"
    assert not dlq_file.exists()


def test_run_dbt_fix_loop_caps_at_three_attempts(tmp_path, monkeypatch) -> None:
    state = MigrationSessionState(
        target_dialect=TargetDialect.DUCKDB,
        checkpoint_approved=True,
        max_fix_attempts=3,
    )

    def _fail(*_args, **_kwargs):
        return 1, "", "boom"

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _fail)
    out = run_dbt_with_fix_loop(tmp_path, state)
    run_attempts = [a for a in out.attempts if a.command == "dbt run"]
    assert len(run_attempts) == 3
    assert out.status.value == "HITL_REQUIRED"


def test_approve_checkpoint_b_resets_and_runs_model(tmp_path, monkeypatch) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    model_name = "orders_model"
    checkpoint_file = checkpoint_dir / f"checkpoint_b_{model_name}.json"
    checkpoint_file.write_text(
        json.dumps(
            {
                "model_name": model_name,
                "current_sql": "select broken",
                "error_log": "syntax error",
                "attempt_history": [],
            }
        ),
        encoding="utf-8",
    )
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    fixed_sql = tmp_path / "fixed.sql"
    fixed_sql.write_text("select 1 as id", encoding="utf-8")
    calls = {"n": 0}

    def _ok(*_args, **_kwargs):
        calls["n"] += 1
        return 0, "ok", ""

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _ok)
    rc, _msg = approve_checkpoint_b_sql(
        dbt_project_dir=tmp_path,
        model_name=model_name,
        fixed_sql_path=fixed_sql,
        checkpoint_dir=checkpoint_dir,
    )
    assert rc == 0
    assert calls["n"] == 1
    assert not checkpoint_file.exists()
    assert (models_dir / f"{model_name}.sql").read_text(encoding="utf-8") == "select 1 as id"


def test_reject_checkpoint_b_routes_to_dlq(tmp_path) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    dlq_dir = tmp_path / "dlq"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    model_name = "orders_model"
    checkpoint_file = checkpoint_dir / f"checkpoint_b_{model_name}.json"
    checkpoint_file.write_text(
        json.dumps(
            {
                "model_name": model_name,
                "current_sql": "select broken",
                "error_log": "column does not exist",
                "attempt_history": [{"timestamp": "2026-03-25T00:00:00+00:00", "error_snippet": "x", "action_taken": "retry"}],
            }
        ),
        encoding="utf-8",
    )
    rc, _msg = reject_checkpoint_b_to_dlq(
        model_name=model_name,
        checkpoint_dir=checkpoint_dir,
        dlq_dir=dlq_dir,
    )
    assert rc == 0
    assert not checkpoint_file.exists()
    dlq_file = dlq_dir / "dlq_records.jsonl"
    assert dlq_file.is_file()
    payload = json.loads(dlq_file.read_text(encoding="utf-8").strip())
    assert payload["error_stage"] == "CHECKPOINT_B_REJECTION"
    for field in ("original_payload", "error_reason", "error_stage", "timestamp", "run_id"):
        assert field in payload


def test_wave_barrier_blocks_next_wave(tmp_path, monkeypatch) -> None:
    calls = {"wave2_started": False}

    def _exec(**kwargs):
        model = kwargs["model_names"][0]
        from ama.dbt_migration.models import ExecutionResult, ModelExecutionTrace, ModelRunState
        if model == "w1_bad":
            return ExecutionResult(
                model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.HITL_REQUIRED)]
            )
        calls["wave2_started"] = calls["wave2_started"] or model.startswith("w2_")
        return ExecutionResult(
            status=RunnerFinalStatus.SUCCESS,
            model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.SUCCESS)],
        )

    monkeypatch.setattr("ama.dbt_migration.service.execute_models_with_fix_loop", _exec)
    state, telemetry, blocked = _orchestrate_waves_with_gating(
        wave_to_model_names={1: ["w1_bad"], 2: ["w2_ok"]},
        dbt_project_dir=tmp_path,
        max_fix_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
        bypass_wave=None,
        stop_on_first_error=False,
    )
    assert blocked is True
    assert state["w1_bad"] == "HITL_REQUIRED"
    assert calls["wave2_started"] is False
    assert telemetry[-1]["current_wave_id"] == 1


def test_wave_bypass_allows_next_wave(tmp_path, monkeypatch) -> None:
    calls = {"wave2_started": False}

    def _exec(**kwargs):
        model = kwargs["model_names"][0]
        from ama.dbt_migration.models import ExecutionResult, ModelExecutionTrace, ModelRunState
        if model == "w1_bad":
            return ExecutionResult(
                model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.HITL_REQUIRED)]
            )
        if model == "w2_ok":
            calls["wave2_started"] = True
        return ExecutionResult(
            status=RunnerFinalStatus.SUCCESS,
            model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.SUCCESS)],
        )

    monkeypatch.setattr("ama.dbt_migration.service.execute_models_with_fix_loop", _exec)
    state, telemetry, blocked = _orchestrate_waves_with_gating(
        wave_to_model_names={1: ["w1_bad"], 2: ["w2_ok"]},
        dbt_project_dir=tmp_path,
        max_fix_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
        bypass_wave=1,
        stop_on_first_error=False,
    )
    assert blocked is False
    assert state["w1_bad"] == "HITL_REQUIRED"
    assert state["w2_ok"] == "SUCCESS"
    assert calls["wave2_started"] is True
    assert telemetry[-1]["current_wave_id"] == 2


def test_hitl_blocking_keeps_next_wave_queued(tmp_path, monkeypatch) -> None:
    touched = {"w2": False}

    def _exec(**kwargs):
        model = kwargs["model_names"][0]
        from ama.dbt_migration.models import ExecutionResult, ModelExecutionTrace, ModelRunState
        if model == "w1_hitl":
            return ExecutionResult(
                model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.HITL_REQUIRED)]
            )
        touched["w2"] = True
        return ExecutionResult(
            status=RunnerFinalStatus.SUCCESS,
            model_results=[ModelExecutionTrace(model_name=model, state=ModelRunState.SUCCESS)],
        )

    monkeypatch.setattr("ama.dbt_migration.service.execute_models_with_fix_loop", _exec)
    _state, _telemetry, blocked = _orchestrate_waves_with_gating(
        wave_to_model_names={1: ["w1_hitl"], 2: ["w2_should_not_run"]},
        dbt_project_dir=tmp_path,
        max_fix_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
        bypass_wave=None,
        stop_on_first_error=False,
    )
    assert blocked is True
    assert touched["w2"] is False


def test_fix_agent_429_falls_back_to_deterministic_retry(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "orders_model.sql").write_text("select missing_col from src", encoding="utf-8")

    calls = {"n": 0}

    def _run(command, _cwd):
        calls["n"] += 1
        if command[:2] == ["dbt", "run"] and calls["n"] == 1:
            return 1, "", "column does not exist: missing_col"
        return 0, "ok", ""

    def _rate_limited(**_kwargs):
        raise OpenAIRateLimitError("429")

    monkeypatch.setenv("AMA_OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _run)
    monkeypatch.setattr("ama.dbt_migration.runner._run_fix_agent", _rate_limited)

    result = execute_models_with_fix_loop(
        dbt_project_dir=tmp_path,
        model_names=["orders_model"],
        max_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
    )
    assert result.status == RunnerFinalStatus.SUCCESS
    assert result.model_results[0].fix_loop_count == 1


def test_fix_agent_corrects_missing_column_sql(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    sql_path = models_dir / "orders_model.sql"
    sql_path.write_text("select missing_col from raw_orders", encoding="utf-8")

    calls = {"n": 0}

    def _run(command, _cwd):
        calls["n"] += 1
        if command[:2] == ["dbt", "run"] and calls["n"] == 1:
            return 1, "", "column does not exist: missing_col"
        return 0, "ok", ""

    def _fixed_sql(**_kwargs):
        return "select order_id from raw_orders", "Column missing; select valid field", 120, 0.93

    monkeypatch.setenv("AMA_OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _run)
    monkeypatch.setattr("ama.dbt_migration.runner._run_fix_agent", _fixed_sql)

    result = execute_models_with_fix_loop(
        dbt_project_dir=tmp_path,
        model_names=["orders_model"],
        max_attempts=3,
        dlq_dir=tmp_path / "dlq",
        checkpoint_dir=tmp_path / "checkpoints",
    )
    assert result.status == RunnerFinalStatus.SUCCESS
    assert sql_path.read_text(encoding="utf-8") == "select order_id from raw_orders"


def test_service_apply_ai_fix_from_checkpoint(tmp_path, monkeypatch) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_dir / "checkpoint_b_orders_model.json"
    checkpoint_file.write_text(
        json.dumps(
            {
                "model_name": "orders_model",
                "current_sql": "select bad from raw_orders",
                "error_log": "column does not exist: bad",
                "attempt_history": [],
                "suggested_sql": "select order_id from raw_orders",
            }
        ),
        encoding="utf-8",
    )

    def _ok(*_args, **_kwargs):
        return 0, "ok", ""

    monkeypatch.setattr("ama.dbt_migration.runner._run_command", _ok)
    rc, _msg = apply_ai_fix_from_checkpoint(
        dbt_project_dir=tmp_path,
        checkpoint_dir=checkpoint_dir,
        model_name="orders_model",
        ai_sql="select order_id from raw_orders",
    )
    assert rc == 0


def test_run_wave_stress_test_persists_insights(tmp_path, monkeypatch) -> None:
    def _summary(wave_id, model_names, model_states):
        return (
            {"health": "stable", "structural_risks": ["none"], "confidence_aggregation": 0.88},
            {"tokens_used": 111, "confidence": 0.88, "is_fallback_active": False},
        )

    monkeypatch.setattr("ama.dbt_migration.service.wave_summary_agent", _summary)
    out = run_wave_stress_test(
        checkpoint_dir=tmp_path,
        wave_id="1",
        model_names=["m1"],
        model_states={"m1": "SUCCESS"},
    )
    assert out["health"] == "stable"
    insights_path = tmp_path / "model_insights.json"
    assert insights_path.is_file()
    payload = json.loads(insights_path.read_text(encoding="utf-8"))
    assert payload["waves"]["1"]["confidence_aggregation"] == 0.88


def test_risk_and_scenario_agents_persist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "ama.dbt_migration.service.risk_agent",
        lambda sql, model_name: (
            {"risk_level": "High", "concerns": ["join explosion"]},
            {"tokens_used": 12, "confidence": 0.9, "is_fallback_active": False},
        ),
    )
    monkeypatch.setattr(
        "ama.dbt_migration.service.scenario_agent",
        lambda sql, model_name: (
            {"scenarios": ["duplicate id", "future date", "null amount"]},
            {"tokens_used": 8, "confidence": 0.8, "is_fallback_active": False},
        ),
    )
    out = analyze_model_risk_and_scenarios(
        checkpoint_dir=tmp_path,
        model_name="orders_model",
        sql="select 1",
    )
    assert out["risk"]["risk_level"] == "High"
    assert len(out["scenarios"]) == 3


def test_generate_synthetic_data_gating_and_chat_proposal(tmp_path, monkeypatch) -> None:
    rc, msg, _ = generate_synthetic_data_for_model(
        checkpoint_dir=tmp_path,
        model_name="orders_model",
        schema_columns=["order_id"],
        approved=False,
        row_cap=10,
    )
    assert rc == 1
    assert "explicit approval" in msg

    monkeypatch.setattr(
        "ama.dbt_migration.service.data_gen_agent",
        lambda model_name, schema_columns, row_count=10: (
            {"complex_mock_data": [{"order_id": "x"}], "confidence": 0.5},
            {"tokens_used": 10, "confidence": 0.5, "is_fallback_active": False},
        ),
    )
    rc2, _msg2, p = generate_synthetic_data_for_model(
        checkpoint_dir=tmp_path,
        model_name="orders_model",
        schema_columns=["order_id"],
        approved=True,
        row_cap=5,
    )
    assert rc2 == 0
    assert Path(p).is_file()

    monkeypatch.setattr(
        "ama.dbt_migration.service.chat_model_agent",
        lambda model_name, sql, question: (
            {"answer": "Use CTE for readability.", "sql_patch_proposal": "with x as (...) select * from x"},
            {"tokens_used": 20, "confidence": 0.77, "is_fallback_active": False},
        ),
    )
    chat = propose_sql_patch_from_chat(
        checkpoint_dir=tmp_path,
        model_name="orders_model",
        sql="select * from t",
        question="why CTE?",
    )
    assert "answer" in chat and "sql_patch_proposal" in chat


def test_job_helpers_persist_and_append_events(tmp_path) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    job_id = "job-1"

    payload = {"job_id": job_id, "status": "RUNNING", "completed_models": 1}
    dbt_service.save_job(checkpoint_dir, job_id, payload)
    loaded = dbt_service.load_job(checkpoint_dir, job_id)
    assert loaded.get("job_id") == job_id
    assert loaded.get("completed_models") == 1

    dbt_service.append_event(checkpoint_dir, job_id, {"event_type": "MODEL_START", "timestamp": "t", "model_name": "m1"})
    events_file = checkpoint_dir / "jobs" / f"{job_id}.events.jsonl"
    assert events_file.is_file()
    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "MODEL_START"


def test_checkpoint_a_job_poll_success(tmp_path) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    job_id = "job-poll-1"

    model_art = ModelArtifact(
        table_key="dbo.demo_table",
        model_name="demo_table",
        sql="select 1 as id",
        schema_yml="version: 2\nmodels: []\n",
    )
    checkpoint_a = CheckpointAArtifact(
        wave_summary="ws",
        generated_models=[model_art],
        mapping_rows=[],
        review_required_tables=[],
        model_insights={"waves": {}, "models": {}, "telemetry": []},
    )

    dbt_service.save_checkpoint_a_for_job(checkpoint_dir, job_id, checkpoint_a)
    dbt_service.save_job(
        checkpoint_dir,
        job_id,
        {"job_id": job_id, "status": "SUCCESS", "completed_models": 1, "total_models": 1, "checkpoint_a_path": ""},
    )

    job, loaded_checkpoint_a = dbt_service.poll_generate_dbt_checkpoint_a_job(
        checkpoint_dir=checkpoint_dir,
        job_id=job_id,
    )
    assert job.get("status") == "SUCCESS"
    assert loaded_checkpoint_a is not None
    assert loaded_checkpoint_a.wave_summary == "ws"
    assert loaded_checkpoint_a.generated_models[0].model_name == "demo_table"


def test_start_generate_checkpoint_a_job_persists_events(tmp_path, monkeypatch) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    report_path = tmp_path / "report.json"
    report_path.write_text("{}", encoding="utf-8")

    # Stub out synchronous generation so the thread finishes quickly.
    def _stub_run_generate_dbt(
        *,
        progress_callback=None,
        job_id=None,
        **_kwargs,
    ):
        if progress_callback is not None:
            progress_callback("JOB_TOTAL", {"total_models": 1})
            progress_callback("MODEL_DONE", {"model_name": "m1", "generation_mode": "legacy", "generation_confidence": 0.9})
        checkpoint_a = CheckpointAArtifact(
            wave_summary="ws",
            generated_models=[
                ModelArtifact(
                    table_key="dbo.t",
                    model_name="m1",
                    sql="select 1",
                    schema_yml="version: 2\nmodels: []\n",
                )
            ],
            mapping_rows=[],
            review_required_tables=[],
            model_insights={"waves": {}, "models": {}, "telemetry": []},
        )
        session = MigrationSessionState(target_dialect=TargetDialect.DUCKDB)
        return session, checkpoint_a, []

    monkeypatch.setattr(dbt_service, "run_generate_dbt", _stub_run_generate_dbt)

    job_id, _payload = dbt_service.start_generate_dbt_checkpoint_a_job(
        report_path=report_path,
        glossary_path=None,
        target_dialect_raw="duckdb",
        dbt_models_dir=tmp_path / "models",
        dbt_project_dir=tmp_path,
        checkpoint_dir=checkpoint_dir,
        dlq_dir=tmp_path / "dlq",
        bypass_wave=None,
        wave_id_filter=None,
        stop_on_first_error=False,
        approve_checkpoint_a=False,
        run_execution=False,
    )

    # Poll until the job finishes.
    checkpoint_a = None
    for _ in range(80):
        job, checkpoint_a = dbt_service.poll_generate_dbt_checkpoint_a_job(
            checkpoint_dir=checkpoint_dir,
            job_id=job_id,
        )
        if str(job.get("status") or "").upper() == "SUCCESS" and checkpoint_a is not None:
            break
        import time

        time.sleep(0.05)

    assert checkpoint_a is not None
    events_file = checkpoint_dir / "jobs" / f"{job_id}.events.jsonl"
    assert events_file.is_file()
    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(json.loads(ln).get("event_type") == "MODEL_DONE" for ln in lines)


def test_data_gen_agent_sanitizes_llm_placeholders(monkeypatch) -> None:
    monkeypatch.setenv("AMA_OPENAI_API_KEY", "fake-key")

    schema_columns = ["invoice_id", "order_id", "amount", "status", "net_amount", "vat_rate"]
    placeholder_rows = []
    for i in range(5):
        placeholder_rows.append(
            {
                "invoice_id": f"sample_{i}",
                "order_id": f"sample_{i}",
                "amount": f"sample_{i}",
                "status": f"sample_{i}",
                "net_amount": f"sample_{i}",
                "vat_rate": f"sample_{i}",
            }
        )

    def _fake_query_openai_json(**_kwargs):
        return AIQueryResult(payload={"complex_mock_data": placeholder_rows}, tokens_used=123)

    monkeypatch.setattr("ama.dbt_migration.cockpit_agents.query_openai_json", _fake_query_openai_json)

    payload, _telemetry = data_gen_agent("orders_model", schema_columns, row_count=5)
    rows = payload.get("complex_mock_data")
    assert isinstance(rows, list)
    assert len(rows) == 5

    for row in rows:
        assert all(not (isinstance(v, str) and v.startswith("sample_")) for v in row.values())
        assert isinstance(row["invoice_id"], str) and row["invoice_id"].startswith("INV-")
        assert isinstance(row["order_id"], str) and row["order_id"].startswith("ORD-")
        # Numeric-ish strings (we keep them as strings for JSON/UI friendliness).
        float(row["amount"])
        float(row["net_amount"])
        float(row["vat_rate"])
        assert isinstance(row["status"], str) and len(row["status"]) >= 3


def test_data_gen_agent_fallback_is_type_realistic(monkeypatch) -> None:
    monkeypatch.delenv("AMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    schema_columns = ["invoice_id", "order_id", "amount", "status", "net_amount", "vat_rate"]
    payload, _telemetry = data_gen_agent("orders_model", schema_columns, row_count=3)
    rows = payload.get("complex_mock_data")
    assert isinstance(rows, list)
    assert len(rows) == 3
    assert all(all(not (isinstance(v, str) and v.startswith("sample_")) for v in row.values()) for row in rows)
    assert rows[0]["invoice_id"].startswith("INV-")
