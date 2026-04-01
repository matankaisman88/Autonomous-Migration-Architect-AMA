from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ama.api.main import app


def _sample_report_path() -> str:
    return str((Path(__file__).resolve().parents[1] / "sample_data" / "scale_engine_chaos" / "chaos_report.json").resolve())


def _load_report(client: TestClient) -> str:
    res = client.post("/report/load", json={"path": _sample_report_path()})
    assert res.status_code == 200, res.text
    return str(res.json()["report_id"])


def _evaluate(client: TestClient, report_id: str) -> dict[str, Any]:
    res = client.post(f"/scale/{report_id}/evaluate", json={})
    assert res.status_code == 200, res.text
    return res.json()


def test_report_load_and_summary() -> None:
    client = TestClient(app)
    load = client.post("/report/load", json={"path": _sample_report_path()})
    assert load.status_code == 200
    payload = load.json()
    assert payload.get("report_id")

    summary = client.get(f"/report/{payload['report_id']}/summary")
    assert summary.status_code == 200
    data = summary.json()
    assert int(data["table_count"]) > 0
    assert isinstance(data["domains"], list) and data["domains"]


def test_scale_evaluate_returns_scored_tables() -> None:
    client = TestClient(app)
    report_id = _load_report(client)
    data = _evaluate(client, report_id)
    table_count = len(data["scored_tables"])
    assert data["would_migrate"] + data["would_flag_review"] + data["would_block"] == table_count
    assert all(row["queue"] in {"green", "yellow", "red"} for row in data["scored_tables"])


def test_explain_returns_full_breakdown() -> None:
    client = TestClient(app)
    report_id = _load_report(client)
    evaluated = _evaluate(client, report_id)
    table_key = str(evaluated["scored_tables"][0]["table_key"])

    res = client.get(f"/scale/{report_id}/explain/{table_key}")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body["confidence"]["score"], int)
    assert isinstance(body["summary"], str) and body["summary"].strip()


def test_propose_returns_sql() -> None:
    client = TestClient(app)
    report_id = _load_report(client)
    evaluated = _evaluate(client, report_id)
    green = next((r for r in evaluated["scored_tables"] if r["queue"] == "green"), evaluated["scored_tables"][0])
    res = client.post(
        f"/migration/{report_id}/propose",
        json={"table_key": green["table_key"], "dialect": "duckdb"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "SELECT" in str(body["sql"]).upper()
    assert str(body["model_name"]).strip()


def test_bulk_start_rejects_non_green_tables(monkeypatch) -> None:
    import ama.api.routes.bulk as bulk_route

    def _noop_run_bulk_job(**kwargs: Any) -> None:
        with bulk_route._BULK_JOBS_LOCK:
            job = bulk_route._BULK_JOBS[str(kwargs["job_id"])]
            job["status"] = "done"

    monkeypatch.setattr(bulk_route, "_run_bulk_job", _noop_run_bulk_job)

    client = TestClient(app)
    report_id = _load_report(client)
    evaluated = _evaluate(client, report_id)
    green = next(r for r in evaluated["scored_tables"] if r["queue"] == "green")
    non_green = next(r for r in evaluated["scored_tables"] if r["queue"] != "green")
    res = client.post(
        f"/bulk/{report_id}/start",
        json={"table_keys": [green["table_key"], non_green["table_key"]], "dialect": "duckdb"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["queued"] == 1
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["table_key"] == non_green["table_key"]


def test_bulk_job_status_lifecycle(monkeypatch) -> None:
    import ama.api.routes.bulk as bulk_route

    def _fast_run_bulk_job(**kwargs: Any) -> None:
        job_id = str(kwargs["job_id"])
        with bulk_route._BULK_JOBS_LOCK:
            bulk_route._BULK_JOBS[job_id]["status"] = "running"
        with bulk_route._BULK_JOBS_LOCK:
            bulk_route._BULK_JOBS[job_id]["status"] = "done"

    monkeypatch.setattr(bulk_route, "_run_bulk_job", _fast_run_bulk_job)

    client = TestClient(app)
    report_id = _load_report(client)
    evaluated = _evaluate(client, report_id)
    green = next(r for r in evaluated["scored_tables"] if r["queue"] == "green")
    start = client.post(f"/bulk/{report_id}/start", json={"table_keys": [green["table_key"]], "dialect": "duckdb"})
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    status = client.get(f"/bulk/job/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] in {"queued", "running", "done"}


def test_agent_turn_is_stateless(monkeypatch) -> None:
    import ama.api.routes.agent as agent_route

    class _StubResult:
        def __init__(self, state: dict[str, Any]) -> None:
            self.status = "final"
            self.message = "ok"
            self.state = state
            self.pending_write = None
            self.tokens_used = 1
            self.cost_est = 0.0

    def _stub_run_agent_turn(**kwargs: Any) -> _StubResult:
        state = dict(kwargs["state"])
        messages = list(state.get("messages", []))
        messages.append({"role": "user", "content": kwargs.get("user_message", "")})
        messages.append({"role": "assistant", "content": "status"})
        state["messages"] = messages
        return _StubResult(state)

    monkeypatch.setattr(agent_route, "run_agent_turn", _stub_run_agent_turn)

    client = TestClient(app)
    report_id = _load_report(client)
    first_state: dict[str, Any] = {"messages": []}
    first = client.post(
        f"/agent/{report_id}/turn",
        json={"user_message": "Show Status", "state": first_state, "dialect": "duckdb"},
    )
    assert first.status_code == 200
    s1 = first.json()["state"]
    assert len(s1["messages"]) > len(first_state["messages"])

    second = client.post(
        f"/agent/{report_id}/turn",
        json={"user_message": "Continue", "state": s1, "dialect": "duckdb"},
    )
    assert second.status_code == 200
    s2 = second.json()["state"]
    assert len(s2["messages"]) > len(s1["messages"])


def test_unknown_report_id_returns_404() -> None:
    client = TestClient(app)
    res = client.get("/report/nonexistent123/summary")
    assert res.status_code == 404


def test_websocket_bulk_progress_sends_state(monkeypatch) -> None:
    import ama.api.routes.bulk as bulk_route

    def _fast_run_bulk_job(**kwargs: Any) -> None:
        job_id = str(kwargs["job_id"])
        with bulk_route._BULK_JOBS_LOCK:
            bulk_route._BULK_JOBS[job_id]["status"] = "done"
            bulk_route._BULK_JOBS[job_id]["current_table"] = ""

    monkeypatch.setattr(bulk_route, "_run_bulk_job", _fast_run_bulk_job)

    client = TestClient(app)
    report_id = _load_report(client)
    evaluated = _evaluate(client, report_id)
    green = next(r for r in evaluated["scored_tables"] if r["queue"] == "green")
    start = client.post(f"/bulk/{report_id}/start", json={"table_keys": [green["table_key"]], "dialect": "duckdb"})
    assert start.status_code == 200
    job_id = start.json()["job_id"]

    with client.websocket_connect(f"/ws/bulk/{job_id}") as ws:
        msg = ws.receive_json()
        assert isinstance(msg, dict)
        assert "status" in msg


def test_discovery_lineage_subgraph() -> None:
    client = TestClient(app)
    report_id = _load_report(client)
    res = client.get(
        "/api/discovery/lineage/dbo.orders",
        params={"report_id": report_id},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert "nodes" in data and "edges" in data
    assert isinstance(data["nodes"], list)

