from __future__ import annotations

import json
from pathlib import Path

from ama.ai_query_helper import AIQueryResult
from ama.migration_agent import agent_tools
from ama.migration_agent.engine import init_state, run_agent_turn


def _write_min_report(path: Path) -> dict:
    report = {
        "migration_context": "finance.invoices",
        "discovery": {
            "inventory": [
                {
                    "full_name": "finance.invoices",
                    "business_domain": "Finance",
                    "priority_score": 0.9,
                    "query_count": 10,
                    "status": "active",
                },
                {
                    "full_name": "finance.payments",
                    "business_domain": "Finance",
                    "priority_score": 0.8,
                    "query_count": 8,
                    "status": "active",
                },
            ]
        },
        "alias_merge": {},
        "importance_ddl": [],
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    return report


def test_migration_agent_wave1_sequence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMA_OPENAI_API_KEY", "fake-key")
    report_path = tmp_path / "report.json"
    report = _write_min_report(report_path)

    calls: list[str] = []

    def _get_summary(**_kwargs):
        calls.append("list_waves")
        return {"waves": [{"wave_id": 1, "tables": ["finance.invoices", "finance.payments"]}]}

    def _inspect_table(*, table, **_kwargs):
        calls.append(f"analyze_schema:{table}")
        return {"table_key": table, "ddl_columns": ["id"], "sample_rows": []}

    def _generate_sql(*, table, **_kwargs):
        calls.append(f"propose_dbt_model:{table}")
        return {"model_name": table.replace(".", "_"), "sql": "select 1"}

    monkeypatch.setattr("ama.migration_agent.agent_tools.list_waves", _get_summary)
    monkeypatch.setattr("ama.migration_agent.agent_tools.analyze_schema", _inspect_table)
    monkeypatch.setattr("ama.migration_agent.agent_tools.propose_dbt_model", _generate_sql)

    replies = [
        {"tool_request": {"name": "list_waves", "args": {}}},
        {"tool_request": {"name": "analyze_schema", "args": {"table": "finance.invoices"}}},
        {"tool_request": {"name": "propose_dbt_model", "args": {"table": "finance.invoices", "dialect": "duckdb"}}},
        {"tool_request": {"name": "analyze_schema", "args": {"table": "finance.payments"}}},
        {"tool_request": {"name": "propose_dbt_model", "args": {"table": "finance.payments", "dialect": "duckdb"}}},
        {"final": {"message": "Wave 1 drafted."}},
    ]
    idx = {"i": 0}

    def _fake_query(**_kwargs):
        out = replies[idx["i"]]
        idx["i"] += 1
        return AIQueryResult(payload=out, tokens_used=10)

    monkeypatch.setattr("ama.migration_agent.engine.query_openai_json", _fake_query)

    state = init_state({})
    result = run_agent_turn(
        state=state,
        report=report,
        report_path=report_path,
        dbt_project_dir=tmp_path,
        output_dir=tmp_path / "models",
        glossary_path=None,
        user_message="Migrate Wave 1",
    )
    assert result.status == "final"
    assert calls == [
        "list_waves",
        "analyze_schema:finance.invoices",
        "propose_dbt_model:finance.invoices",
        "analyze_schema:finance.payments",
        "propose_dbt_model:finance.payments",
    ]


def test_migration_agent_failed_test_calls_apply_fix_before_reapprove(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMA_OPENAI_API_KEY", "fake-key")
    report_path = tmp_path / "report.json"
    report = _write_min_report(report_path)

    first_turn = [
        {
            "tool_request": {
                "name": "request_write_permission",
                "args": {"model": "finance_invoices", "sql": "select bad_col from t"},
            }
        }
    ]
    second_turn = [
        {
            "tool_request": {
                "name": "apply_fix",
                "args": {
                    "model": "finance_invoices",
                    "error_log": "column does not exist: bad_col",
                    "attempt_history": [],
                },
            }
        },
        {
            "tool_request": {
                "name": "request_write_permission",
                "args": {"model": "finance_invoices", "sql": "select order_id from t"},
            }
        },
    ]
    turn_idx = {"phase": 0, "i": 0}
    apply_calls: list[str] = []

    def _fake_apply_fix(**_kwargs):
        apply_calls.append("apply_fix")
        return {
            "corrected_sql": "select order_id from t",
            "error_analysis": "bad column removed",
            "success": True,
        }

    monkeypatch.setattr("ama.migration_agent.agent_tools.apply_fix", _fake_apply_fix)

    def _fake_query(**_kwargs):
        if turn_idx["phase"] == 0:
            payload = first_turn[turn_idx["i"]]
            turn_idx["i"] += 1
            if turn_idx["i"] >= len(first_turn):
                turn_idx["phase"] = 1
                turn_idx["i"] = 0
            return AIQueryResult(payload=payload, tokens_used=5)
        payload = second_turn[turn_idx["i"]]
        turn_idx["i"] += 1
        return AIQueryResult(payload=payload, tokens_used=5)

    monkeypatch.setattr("ama.migration_agent.engine.query_openai_json", _fake_query)

    state = init_state({})
    first = run_agent_turn(
        state=state,
        report=report,
        report_path=report_path,
        dbt_project_dir=tmp_path,
        output_dir=tmp_path / "models",
        glossary_path=None,
        user_message="Migrate Wave 1",
    )
    assert first.status == "pending_write"
    assert state.get("pending_write", {}).get("model_name") == "finance_invoices"

    state["pending_write"] = None
    resumed = run_agent_turn(
        state=state,
        report=report,
        report_path=report_path,
        dbt_project_dir=tmp_path,
        output_dir=tmp_path / "models",
        glossary_path=None,
        tool_result_message={
            "tool_name": "commit_to_disk",
            "approved": True,
            "test_result": {
                "success": False,
                "logs": "column does not exist: bad_col",
                "return_code": 1,
            },
        },
    )
    assert resumed.status == "pending_write"
    assert apply_calls == ["apply_fix"]
    assert state.get("pending_write", {}).get("sql") == "select order_id from t"


def test_inspect_table_missing_duckdb_is_non_blocking_when_ddl_exists(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    manifest_path = tmp_path / "manifest.json"
    ddl_path = tmp_path / "finance_invoices.json"
    ddl_path.write_text(json.dumps({"columns": ["invoice_id", "amount"]}), encoding="utf-8")
    manifest_path.write_text(json.dumps({"finance.invoices": str(ddl_path)}), encoding="utf-8")
    report = {
        "alias_merge": {"ddl_manifest": str(manifest_path)},
        "importance_ddl": [{"source_table": "finance.invoices", "column": "invoice_id"}],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    payload = agent_tools.inspect_table(
        report=report,
        report_path=report_path,
        table_key="finance.invoices",
        duckdb_path=tmp_path / "missing.db",
        sample_row_cap=10,
    )
    assert payload["non_blocking"] is True
    assert payload["sample_rows_available"] is False
    assert "sample_rows_warning" in payload
    assert "error" not in payload


def test_get_tools_contains_required_migration_agent_functions() -> None:
    names = [x["function"]["name"] for x in agent_tools.get_tools()]
    assert names == [
        "list_waves",
        "analyze_schema",
        "propose_dbt_model",
        "execute_dbt_test",
        "apply_fix",
        "request_write_permission",
    ]
