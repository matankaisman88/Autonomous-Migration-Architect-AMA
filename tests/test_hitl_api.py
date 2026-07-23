"""API tests for column mapping review (HITL) endpoints."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ama.api.main import app
from ama.business_logic import review_row_signature


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def demo_report_id(tmp_path: Path, client: TestClient) -> str:
    src = Path(__file__).resolve().parents[1] / "sample_data" / "dashboard" / "demo_with_review.json"
    dst = tmp_path / "demo_with_review.json"
    shutil.copy(src, dst)
    res = client.post("/report/load", json={"path": str(dst)})
    assert res.status_code == 200, res.text
    report_id = str(res.json()["report_id"])
    yield report_id
    sidecar = dst.with_suffix(".hitl.json")
    if sidecar.is_file():
        sidecar.unlink()


def test_hitl_queue_returns_pending_items(client: TestClient, demo_report_id: str) -> None:
    res = client.get(f"/hitl/{demo_report_id}/queue")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pending_count"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["status"] == "pending"


def test_hitl_decision_auto_applies_and_reduces_pending(client: TestClient, demo_report_id: str) -> None:
    queue = client.get(f"/hitl/{demo_report_id}/queue").json()
    row = queue["items"][0]["row"]
    res = client.post(
        f"/hitl/{demo_report_id}/decision",
        json={"row": row, "action": "approved", "auto_apply": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["saved"] is True
    assert body.get("applied") is True
    assert body["pending_count"] == 1

    summary = client.get(f"/report/{demo_report_id}/summary").json()
    assert summary["pending_review_count"] == 1


def test_hitl_batch_approve_by_min_confidence(client: TestClient, demo_report_id: str) -> None:
    res = client.post(
        f"/hitl/{demo_report_id}/decisions/batch",
        json={"action": "approved", "min_confidence": 0.0, "auto_apply": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["matched"] == 2
    queue = client.get(f"/hitl/{demo_report_id}/queue").json()
    assert queue["pending_count"] == 0


def test_hitl_queue_filter_by_source_table(client: TestClient, demo_report_id: str) -> None:
    queue = client.get(f"/hitl/{demo_report_id}/queue").json()
    table = str(queue["items"][0]["row"]["source_table"])
    filtered = client.get(f"/hitl/{demo_report_id}/queue", params={"source_table": table}).json()
    assert len(filtered["items"]) == 2
    assert all(str(i["row"].get("source_table")) == table for i in filtered["items"])


def test_hitl_reject_moves_to_trash_on_apply(client: TestClient, demo_report_id: str) -> None:
    queue = client.get(f"/hitl/{demo_report_id}/queue").json()
    row = queue["items"][0]["row"]
    sig = review_row_signature(row)
    res = client.post(
        f"/hitl/{demo_report_id}/decision",
        json={"row": row, "action": "rejected", "auto_apply": True},
    )
    assert res.status_code == 200
    assert res.json()["signature"] == sig
    assert res.json()["counts"]["trash_candidates"] == 1

    after = client.get(f"/hitl/{demo_report_id}/queue").json()
    assert after["rejected_count"] == 1
    assert len(after["rejected_items"]) == 1


def test_hitl_writes_to_store_when_report_dir_readonly(
    tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ama.api import deps

    src = Path(__file__).resolve().parents[1] / "sample_data" / "dashboard" / "demo_with_review.json"
    report_path = tmp_path / "fixture" / "demo_with_review.json"
    report_path.parent.mkdir(parents=True)
    shutil.copy(src, report_path)
    store_dir = tmp_path / "hitl_store"
    monkeypatch.setattr(deps, "_parent_writable", lambda _path: False)
    monkeypatch.setattr(deps, "get_hitl_store_dir", lambda: store_dir)

    load = client.post("/report/load", json={"path": str(report_path)})
    assert load.status_code == 200
    report_id = str(load.json()["report_id"])

    queue = client.get(f"/hitl/{report_id}/queue").json()
    row = queue["items"][0]["row"]
    res = client.post(
        f"/hitl/{report_id}/decision",
        json={"row": row, "action": "approved", "auto_apply": True},
    )
    assert res.status_code == 200, res.text
    sidecar = store_dir / f"{report_id}.hitl.json"
    assert sidecar.is_file()
    assert not (report_path.with_suffix(".hitl.json")).is_file()
