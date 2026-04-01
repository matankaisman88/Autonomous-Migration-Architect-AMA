from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ama.api import deps
from ama.bulk_runner import _BULK_JOBS, _BULK_JOBS_LOCK, _bulk_job_clear, _bulk_job_write, _run_bulk_job
from ama.scale_engine import evaluate_batch

logger = logging.getLogger(__name__)
router = APIRouter()


class BulkStartRequest(BaseModel):
    table_keys: list[str]
    dialect: str = "duckdb"
    conf_floor: int = 70
    crit_ceil: int = 40
    approved_by: str = "api"
    max_workers: int = Field(default=4, ge=1, le=16)
    dbt_workers: int = Field(default=1, ge=1, le=8)
    dry_run: bool | None = None


@router.post("/{report_id}/start")
def bulk_start(report_id: str, body: BulkStartRequest) -> dict[str, Any]:
    """Evaluate selected tables, queue GREEN-only work, and start _run_bulk_job in background."""
    report = deps.get_report(report_id)
    report_path = deps.get_report_path(report_id)
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)
    if body.dry_run is not None:
        raise HTTPException(status_code=400, detail="dry_run is not allowed for this endpoint")
    if not body.table_keys:
        raise HTTPException(status_code=400, detail="table_keys is required")

    try:
        eval_res = evaluate_batch(report=report, dry_run=True, conf_floor=body.conf_floor, crit_ceil=body.crit_ceil)
    except Exception as exc:
        logger.exception("bulk evaluate failed")
        raise HTTPException(status_code=500, detail=f"Bulk evaluate failed: {exc}") from exc
    score_by_table = {s.table_key: s for s in eval_res.scored_tables}

    runnable: list[str] = []
    skipped: list[dict[str, str]] = []
    for key in body.table_keys:
        scored = score_by_table.get(key)
        if scored is None:
            skipped.append({"table_key": key, "reason": "table not found"})
            continue
        if scored.queue != "green":
            skipped.append({"table_key": key, "reason": f"queue={scored.queue} is not GREEN"})
            continue
        runnable.append(key)

    scored_rows = {
        s.table_key: {
            "queue": s.queue,
            "confidence_result": s.confidence_result,
            "criticality_result": s.criticality_result,
            "anomaly_flags": s.anomaly_flags,
        }
        for s in eval_res.scored_tables
        if s.table_key in runnable
    }

    job_id = str(uuid.uuid4())
    with _BULK_JOBS_LOCK:
        _BULK_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "total": len(runnable),
            "completed": 0,
            "current_table": "",
            "success": [],
            "failed": [],
            "error": "",
        }
        _bulk_job_write(dbt_project_dir=dbt_project_dir, job_id=job_id, payload=dict(_BULK_JOBS[job_id]))

    if runnable:
        t = threading.Thread(
            target=_run_bulk_job,
            kwargs={
                "job_id": job_id,
                "table_keys": runnable,
                "report": report,
                "report_path": report_path,
                "dialect": body.dialect,
                "dbt_project_dir": dbt_project_dir,
                "output_dir": output_dir,
                "contract_id": eval_res.contract_preview.contract_id,
                "scored_rows": scored_rows,
                "max_workers": body.max_workers,
                "dbt_workers": body.dbt_workers,
                "dbt_target": None,
            },
            daemon=True,
        )
        t.start()

    return {"job_id": job_id, "queued": len(runnable), "skipped": skipped}


@router.get("/job/{job_id}")
def bulk_job_status(job_id: str) -> dict[str, Any]:
    """Return the current in-memory bulk job state for a job id."""
    with _BULK_JOBS_LOCK:
        state = _BULK_JOBS.get(job_id)
        if not isinstance(state, dict):
            raise HTTPException(status_code=404, detail="job not found")
        return dict(state)


@router.delete("/job/{job_id}")
def bulk_job_clear(job_id: str) -> dict[str, bool]:
    """Clear a bulk job from memory and persisted job file store."""
    _bulk_job_clear(dbt_project_dir=deps.get_dbt_project_dir(), job_id=job_id)
    return {"cleared": True}

