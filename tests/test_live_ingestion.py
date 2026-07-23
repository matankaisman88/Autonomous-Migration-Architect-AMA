"""Live ingestion artifacts and WebSocket progress."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ama.api.live_jobs import live_job_create, live_job_snapshot, live_job_update
from ama.api.main import app
from ama.api.routes.live_connection import (
    LiveStartRequest,
    _live_report_arg_namespace,
    _run_live_worker,
    _write_real_artifacts,
)
from ama.mcp.base import ColumnInfo, TableSchema
from ama.mcp.extraction import LogExtractionResult


def test_write_real_artifacts_layout(tmp_path: Path) -> None:
    tables = {
        "sales.orders": TableSchema(
            schema_name="sales",
            table_name="orders",
            columns=[ColumnInfo(name="id", data_type="int")],
        )
    }
    log_result = LogExtractionResult(
        records=[{"env": "prod", "dialect": "tsql", "sql": "SELECT 1"}],
        source="query_store",
        date_range_applied=True,
        warnings=["SQL literals redacted before export"],
    )
    ctx, ddl_path = _write_real_artifacts(
        tmp_path,
        tables,
        log_result,
        ["sales"],
        lambda _m: None,
    )
    assert ctx == "sales.orders"
    assert ddl_path.name == "sales_orders.json"
    man = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert man["sales.orders"] == "ddl/sales_orders.json"
    assert man["_extraction_meta"]["log_source"] == "query_store"
    row = json.loads((tmp_path / "sql_logs" / "prod.jsonl").read_text(encoding="utf-8").strip())
    assert row["sql"] == "SELECT 1"


def test_real_extract_report_namespace_omits_bundled_sample_context(tmp_path: Path) -> None:
    out = tmp_path / "live_data" / "demo"
    (out / "ddl").mkdir(parents=True)
    (out / "sql_logs").mkdir(parents=True)
    (out / "manifest.json").write_text("{}", encoding="utf-8")
    ddl = out / "ddl" / "dbo_orders.json"
    ddl.write_text('{"columns": ["order_id"]}', encoding="utf-8")
    ns = _live_report_arg_namespace(
        out,
        out / "ama_live_report.json",
        ddl_fallback=ddl,
        migration_context="dbo.orders",
    )
    assert ns.glossary is None
    assert ns.glossary_dirty is None
    assert ns.comms_dir is None
    assert ns.git_root is None
    assert ns.no_glossary is True


def test_live_start_rejects_oracle() -> None:
    client = TestClient(app)
    res = client.post(
        "/api/live/start",
        json={
            "mode": "oracle",
            "connection_name": "x",
            "host": "h",
            "port": 1521,
            "user": "u",
            "password": "p",
        },
    )
    assert res.status_code == 400
    assert "requires mode=sqlserver" in res.json()["detail"]


def test_run_live_worker_real_extract_no_deploy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ama.api.routes.live_connection.default_data_root", lambda: tmp_path)

    provider = MagicMock()
    provider.ping.return_value = True
    provider.extract_ddl.return_value = {
        "dbo.t": TableSchema(
            schema_name="dbo",
            table_name="t",
            columns=[ColumnInfo(name="id", data_type="int")],
        )
    }
    provider.extract_logs.return_value = LogExtractionResult(
        records=[],
        source="plan_cache",
        date_range_applied=False,
        warnings=["No SQL text extracted from the database"],
    )
    monkeypatch.setattr(
        "ama.api.routes.live_connection.get_schema_provider",
        lambda **kwargs: provider,
    )
    monkeypatch.setattr("ama.api.routes.live_connection._run_live_report_build", lambda *a, **kw: (None, None))

    body = LiveStartRequest(
        mode="sqlserver",
        connection_name="real-test",
        host="127.0.0.1",
        port=1433,
        user="sa",
        password="x",
        database="db",
        build_report=False,
    )
    job_id = live_job_create({"connection_name": "real-test"})
    _run_live_worker(job_id=job_id, body=body)

    provider.extract_ddl.assert_called_once()
    provider.extract_logs.assert_called_once()
    snap = live_job_snapshot(job_id)
    assert snap is not None
    assert snap["status"] == "partial"
    assert (tmp_path / "live_data" / "real-test" / "manifest.json").is_file()


def test_live_start_all_schemas_rejects_with_explicit_schemas() -> None:
    with pytest.raises(ValueError, match="all_schemas"):
        LiveStartRequest(
            mode="sqlserver",
            connection_name="x",
            host="127.0.0.1",
            port=1433,
            user="sa",
            password="x",
            database="db",
            all_schemas=True,
            schemas=["dbo"],
        )


def test_run_live_worker_real_extract_all_schemas(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ama.api.routes.live_connection.default_data_root", lambda: tmp_path)
    provider = MagicMock()
    provider.ping.return_value = True
    provider.extract_ddl.return_value = {
        "dbo.t": TableSchema(schema_name="dbo", table_name="t", columns=[ColumnInfo(name="id", data_type="int")]),
        "finance.i": TableSchema(
            schema_name="finance", table_name="i", columns=[ColumnInfo(name="id", data_type="int")]
        ),
    }
    provider.extract_logs.return_value = LogExtractionResult(
        records=[{"env": "prod", "dialect": "tsql", "sql": "SELECT 1"}],
        source="plan_cache",
        date_range_applied=True,
        warnings=[],
    )
    monkeypatch.setattr(
        "ama.api.routes.live_connection.get_schema_provider",
        lambda **kwargs: provider,
    )
    monkeypatch.setattr("ama.api.routes.live_connection._run_live_report_build", lambda *a, **kw: (None, None))

    body = LiveStartRequest(
        mode="sqlserver",
        connection_name="all-schema-test",
        host="127.0.0.1",
        port=1433,
        user="sa",
        password="x",
        database="db",
        all_schemas=True,
        build_report=False,
    )
    job_id = live_job_create({"connection_name": "all-schema-test"})
    _run_live_worker(job_id=job_id, body=body)

    provider.extract_ddl.assert_called_once_with(all_schemas=True)
    provider.extract_logs.assert_called_once()
    assert provider.extract_logs.call_args.kwargs.get("schemas") is None
    man = json.loads((tmp_path / "live_data" / "all-schema-test" / "manifest.json").read_text(encoding="utf-8"))
    assert man["_extraction_meta"]["all_schemas"] is True
    assert "dbo" in man["_extraction_meta"]["schemas_discovered"]


def test_ws_live_reaches_terminal_snapshot() -> None:
    jid = live_job_create(
        {
            "status": "running",
            "stage": "working",
            "percent": 20,
            "log_lines": ["start"],
            "errors": [],
        }
    )
    live_job_update(
        jid,
        status="success",
        stage="done",
        percent=100,
        log_lines=["start", "end"],
        errors=[],
        report_path="/tmp/ama_live_report.json",
        report_build_error=None,
    )
    client = TestClient(app)
    with client.websocket_connect(f"/ws/live/{jid}") as ws:
        first = ws.receive_json()
        assert first["percent"] == 100
        assert first["status"] == "success"
        second = ws.receive_json()
        assert second["status"] == "success"
        assert second.get("report_path") == "/tmp/ama_live_report.json"
