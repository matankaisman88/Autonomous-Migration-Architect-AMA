"""Live ingestion artifacts and WebSocket progress."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ama.api.live_jobs import live_job_create, live_job_update
from ama.api.main import app
from ama.api.routes.live_connection import _write_artifacts


def test_write_live_artifacts(tmp_path: Path) -> None:
    _write_artifacts(tmp_path, 30, lambda _m: None)
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "ddl" / "dbo_orders.json").is_file()
    man = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "dbo.orders" in man
    lines = (tmp_path / "sql_logs" / "prod.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 30


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
