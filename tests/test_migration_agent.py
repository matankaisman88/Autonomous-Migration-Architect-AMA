from __future__ import annotations

import json
from pathlib import Path

import pytest

from ama.ai_query_helper import AIQueryResult
from ama.migration_agent import agent_tools
from ama.migration_agent.engine import init_state, run_agent_turn

ROOT = Path(__file__).resolve().parents[1]


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
        "generate_synthetic_rows",
        "validate_sql_on_duckdb",
        "list_live_tables",
        "explain_sql_live",
        "query_inventory",
        "bulk_migrate_tables",
        "explain_table_score",
    ]


def test_test_model_runs_dbt_run_before_tests(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        calls.append(command)
        if command[:2] == ["dbt", "run"]:
            return 1, "", "Compilation Error: invalid type DECIMAL1"
        return 0, "ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="finance_payments")
    assert out["success"] is False
    assert out["stage"] == "dbt_run"
    assert "DECIMAL1" in str(out["logs"])
    assert calls == [["dbt", "run", "--select", "finance_payments"]]


def test_test_model_runs_tests_only_after_successful_run(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        calls.append(command)
        if command[:2] == ["dbt", "run"]:
            return 0, "run ok", ""
        return 0, "test ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="finance_payments")
    assert out["success"] is True
    assert out["stage"] == "dbt_test"
    assert out["run_return_code"] == 0
    assert calls == [
        ["dbt", "run", "--select", "finance_payments"],
        ["dbt", "test", "--select", "finance_payments"],
    ]


def test_test_model_falls_back_when_target_missing(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        calls.append(command)
        if command == ["dbt", "run", "--select", "finance_payments", "--target", "duckdb"]:
            return 1, "", "The profile 'default' does not have a target named 'duckdb'"
        return 0, "ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="finance_payments", target="duckdb")
    assert out["success"] is True
    assert out["target_fallback_used"] is True
    assert calls == [
        ["dbt", "run", "--select", "finance_payments", "--target", "duckdb"],
        ["dbt", "run", "--select", "finance_payments"],
        ["dbt", "test", "--select", "finance_payments"],
    ]


def test_test_model_sanitizes_legacy_schema_descriptions(monkeypatch, tmp_path: Path) -> None:
    models_dir = tmp_path / "models" / "ama_generated"
    models_dir.mkdir(parents=True, exist_ok=True)
    schema = models_dir / "broken.schema.yml"
    schema.write_text(
        "\n".join(
            [
                "version: 2",
                "models:",
                "  - name: broken",
                "    columns:",
                "      - name: ds_col_1",
                "        description: Source column `{'name': 'ds_col_1', 'type': 'nvarchar'}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        return 0, "ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="finance_payments", target="dev")
    assert out["success"] is True
    repaired = schema.read_text(encoding="utf-8")
    assert "description: \"Source column `{'name': 'ds_col_1', 'type': 'nvarchar'}`\"" in repaired


def test_test_model_sanitizes_trailing_semicolon_sql(monkeypatch, tmp_path: Path) -> None:
    models_dir = tmp_path / "models" / "ama_generated"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_sql = models_dir / "crm_chaos_green_068.sql"
    model_sql.write_text("select * from crm.chaos_green_068;\n", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        calls.append(command)
        return 0, "ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="crm_chaos_green_068", target="dev")
    assert out["success"] is True
    assert model_sql.read_text(encoding="utf-8") == "select * from crm.chaos_green_068\n"
    assert calls[0] == ["dbt", "run", "--select", "crm_chaos_green_068", "--target", "dev"]


def test_test_model_retries_duckdb_lock_error(monkeypatch, tmp_path: Path) -> None:
    call_count = {"n": 0}

    def _fake_run_command(command: list[str], _cwd: Path) -> tuple[int, str, str]:
        call_count["n"] += 1
        if command[:2] == ["dbt", "run"] and call_count["n"] == 1:
            return 1, "", (
                'IO Error: Cannot open file "\\\\?\\C:\\Autonomous-Migration-Architect-AMA\\target\\duckdb.db": '
                "The process cannot access the file because it is being used by another process."
            )
        return 0, "ok", ""

    monkeypatch.setattr("ama.migration_agent.agent_tools._run_command", _fake_run_command)
    monkeypatch.setattr("ama.migration_agent.agent_tools.time.sleep", lambda _s: None)
    out = agent_tools.test_model(dbt_project_dir=tmp_path, model_name="finance_payments", target="dev")
    assert out["success"] is True
    # First run fails with lock, second run succeeds, then test succeeds.
    assert call_count["n"] == 3


def test_extract_source_helpers() -> None:
    sql = (
        "WITH source_data AS (SELECT ds_col_1, ds_col_2 FROM operations.chaos_green_081) "
        "SELECT * FROM source_data"
    )
    rels = agent_tools._extract_source_relations(sql)
    cols = agent_tools._extract_source_columns(sql)
    assert ("operations", "chaos_green_081") in rels
    assert cols == ["ds_col_1", "ds_col_2"]


def test_extract_source_columns_from_cast_syntax() -> None:
    sql = (
        "SELECT customer_id::VARCHAR AS customer_id, email::VARCHAR AS email "
        "FROM dbo.customers"
    )
    cols = agent_tools._extract_source_columns(sql)
    assert "customer_id" in cols
    assert "email" in cols


def test_collect_source_columns_unions_checkpoint_sql_with_ddl(tmp_path: Path) -> None:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "finance_invoices.json").write_text(
        json.dumps({"columns": ["invoice_id", "vat_rate", "vat_amount", "amount", "status"]}),
        encoding="utf-8",
    )
    report_path = tmp_path / "ama_live_report.json"
    report_path.write_text("{}", encoding="utf-8")
    report = {"importance_ddl": [{"source_table": "finance.invoices", "column": "amount"}]}

    class _Art:
        def __init__(self) -> None:
            self.table_key = "finance.invoices"
            self.schema_yml = "version: 2\nmodels:\n  - name: finance_invoices\n    columns:\n      - name: invoice_id\n"
            self.sql = (
                "SELECT invoice_id::VARCHAR, vat_rate::DECIMAL, vat_amount::DECIMAL "
                "FROM finance.invoices"
            )

    cols_map = agent_tools.collect_source_columns_for_artifacts(
        artifacts=[_Art()],
        report=report,
        report_path=report_path,
    )
    cols = cols_map.get("finance.invoices") or []
    assert "amount" in cols
    assert "vat_rate" in cols
    assert "invoice_id" in cols


def test_bootstrap_batch_creates_full_finance_invoices_table(tmp_path: Path) -> None:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "finance_invoices.json").write_text(
        json.dumps(
            {
                "columns": [
                    "invoice_id",
                    "order_id",
                    "amount",
                    "net_amount",
                    "vat_amount",
                    "vat_rate",
                    "status",
                    "due_date",
                    "created_at",
                ]
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "ama_live_report.json"
    report_path.write_text("{}", encoding="utf-8")

    class _Art:
        table_key = "finance.invoices"
        schema_yml = ""
        sql = (
            "SELECT invoice_id::VARCHAR, vat_rate::DECIMAL, vat_amount::DECIMAL "
            "FROM finance.invoices"
        )

    created = agent_tools.bootstrap_duckdb_sources_for_artifacts(
        dbt_project_dir=tmp_path,
        artifacts=[_Art()],
        report={"importance_ddl": [{"source_table": "finance.invoices", "column": "amount"}]},
        report_path=report_path,
    )
    assert created >= 1
    import duckdb

    con = duckdb.connect(str(tmp_path / "target" / "duckdb.db"))
    try:
        names = {
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'finance' AND table_name = 'invoices'"
            ).fetchall()
        }
        assert "vat_rate" in names
        assert "invoice_id" in names
    finally:
        con.close()


def test_resolve_bootstrap_columns_merges_sparse_importance_with_ddl(tmp_path: Path) -> None:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "finance_invoices.json").write_text(
        json.dumps(
            {
                "columns": [
                    "invoice_id",
                    "order_id",
                    "amount",
                    "net_amount",
                    "vat_amount",
                    "vat_rate",
                    "status",
                    "due_date",
                    "created_at",
                ]
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "ama_live_report.json"
    report_path.write_text("{}", encoding="utf-8")
    report = {
        "importance_ddl": [
            {"source_table": "finance.invoices", "column": "amount"},
            {"source_table": "finance.invoices", "column": "status"},
        ]
    }
    sql = (
        "SELECT invoice_id::VARCHAR, vat_rate::DECIMAL, vat_amount::DECIMAL "
        "FROM finance.invoices"
    )
    cols = agent_tools._resolve_bootstrap_columns(
        report=report,
        report_path=report_path,
        table_key="finance.invoices",
        sql_text=sql,
    )
    assert "amount" in cols
    assert "vat_rate" in cols
    assert "invoice_id" in cols


def test_resolve_bootstrap_columns_from_live_data_ddl() -> None:
    report_path = ROOT / "live_data" / "test-conn" / "ama_live_report.json"
    if not report_path.is_file():
        pytest.skip("live_data test-conn report not present")
    cols = agent_tools._resolve_bootstrap_columns(
        report={},
        report_path=report_path,
        table_key="dbo.customers",
    )
    assert "customer_id" in cols
    assert "customer_name" in cols
    assert "email" in cols


def test_proposed_customers_sql_runs_after_report_ddl_bootstrap(tmp_path: Path) -> None:
    try:
        import duckdb  # type: ignore  # noqa: F401
    except Exception:
        return
    report_path = ROOT / "live_data" / "test-conn" / "ama_live_report.json"
    if not report_path.is_file():
        pytest.skip("live_data test-conn report not present")
    proposed = (
        "WITH customer_data AS (SELECT "
        "customer_id::VARCHAR AS customer_id, "
        "customer_name::VARCHAR AS customer_name, "
        "email::VARCHAR AS email, "
        "city::VARCHAR AS city, "
        "country_code::VARCHAR AS country_code, "
        "phone::VARCHAR AS phone, "
        "is_active::BOOLEAN AS is_active, "
        "created_at::TIMESTAMP AS created_at "
        "FROM dbo.customers) SELECT * FROM customer_data"
    )
    model_dir = tmp_path / "models" / "ama_generated"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "dbo_customers.sql").write_text(proposed, encoding="utf-8")
    created = agent_tools._ensure_duckdb_sources_for_model(
        dbt_project_dir=tmp_path,
        model_name="dbo_customers",
        report_path=report_path,
        primary_table_key="dbo.customers",
    )
    assert created >= 1
    con = duckdb.connect(str(tmp_path / "target" / "duckdb.db"))
    try:
        con.execute(proposed)
        cols = [
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'dbo' AND table_name = 'customers'"
            ).fetchall()
        ]
        assert "customer_id" in cols
        assert "email" in cols
    finally:
        con.close()


def test_ensure_duckdb_sources_for_model_creates_missing_relation(tmp_path: Path) -> None:
    try:
        import duckdb  # type: ignore  # noqa: F401
    except Exception:
        return
    model_name = "operations_chaos_green_081"
    model_dir = tmp_path / "models" / "ama_generated"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / f"{model_name}.sql").write_text(
        "SELECT ds_col_1, ds_col_2 FROM operations.chaos_green_081\n",
        encoding="utf-8",
    )
    created = agent_tools._ensure_duckdb_sources_for_model(
        dbt_project_dir=tmp_path,
        model_name=model_name,
    )
    assert created >= 1
    import duckdb  # type: ignore

    con = duckdb.connect(str(tmp_path / "target" / "duckdb.db"))
    try:
        rows = con.execute(
            "select count(*) from information_schema.tables "
            "where table_schema='operations' and table_name='chaos_green_081'"
        ).fetchone()
        assert rows and int(rows[0]) == 1
    finally:
        con.close()
