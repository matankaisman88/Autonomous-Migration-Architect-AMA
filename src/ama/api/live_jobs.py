"""In-memory live ingestion jobs (WebSocket progress)."""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any

_LIVE_JOBS_LOCK = threading.Lock()
_LIVE_JOBS: dict[str, dict[str, Any]] = {}
_LIVE_SEM = threading.Semaphore(3)


def live_job_create(initial: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex[:16]
    payload = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "log_lines": [],
        "errors": [],
        **initial,
    }
    with _LIVE_JOBS_LOCK:
        _LIVE_JOBS[job_id] = payload
    return job_id


def live_job_update(job_id: str, **kwargs: Any) -> None:
    with _LIVE_JOBS_LOCK:
        cur = _LIVE_JOBS.get(job_id)
        if not isinstance(cur, dict):
            return
        cur.update(kwargs)


def live_job_append_log(job_id: str, line: str) -> None:
    with _LIVE_JOBS_LOCK:
        cur = _LIVE_JOBS.get(job_id)
        if not isinstance(cur, dict):
            return
        lines = cur.setdefault("log_lines", [])
        if isinstance(lines, list):
            lines.append(line)
            cur["log_lines"] = lines[-400:]


def live_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _LIVE_JOBS_LOCK:
        cur = _LIVE_JOBS.get(job_id)
        if not isinstance(cur, dict):
            return None
        return dict(cur)


def sanitize_connection_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (name or "").strip())[:80]
    return s or "default"


def mark_all_live_jobs_shutdown() -> None:
    with _LIVE_JOBS_LOCK:
        for j in _LIVE_JOBS.values():
            if str(j.get("status")) in {"queued", "running"}:
                j["status"] = "failure"
                j["stage"] = "interrupted"
                j.setdefault("errors", []).append("Server shutdown")
