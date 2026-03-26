from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ama.api import deps
from ama.dbt_migration.runner import reject_checkpoint_b_to_dlq
from ama.dbt_migration.service import (
    analyze_model_risk_and_scenarios,
    apply_ai_fix_from_checkpoint,
    generate_synthetic_data_for_model,
    poll_generate_dbt_checkpoint_a_job,
    propose_sql_patch_from_chat,
    run_wave_stress_test,
    start_generate_dbt_checkpoint_a_job,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class CheckpointAStartRequest(BaseModel):
    target_dialect: str = "duckdb"
    bypass_wave: int | None = None
    wave_id_filter: int | None = None
    stop_on_first_error: bool = False
    approve_checkpoint_a: bool = False
    run_execution: bool = False


class ModelRiskRequest(BaseModel):
    model_name: str
    sql: str


class SyntheticDataRequest(BaseModel):
    model_name: str
    schema_columns: list[str]
    approved: bool = False
    row_cap: int = Field(default=20, ge=1, le=50)


class SqlPatchRequest(BaseModel):
    model_name: str
    sql: str
    question: str


class WaveStressRequest(BaseModel):
    wave_id: str
    model_names: list[str]
    model_states: dict[str, str]


class ApplyFixRequest(BaseModel):
    model_name: str
    ai_sql: str


class RejectCheckpointRequest(BaseModel):
    model_name: str


@router.post("/{report_id}/checkpoint-a/start")
def cockpit_start_checkpoint_a(report_id: str, body: CheckpointAStartRequest) -> dict[str, Any]:
    """Start async Checkpoint-A generation job via dbt_migration.service orchestration."""
    report_path = deps.get_report_path(report_id)
    checkpoint_dir = deps.get_checkpoint_dir()
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)
    dlq_dir = deps.get_dlq_dir()
    glossary_path = report_path.parent / "glossary.json"
    if not glossary_path.is_file():
        glossary_path = None
    try:
        job_id, job_payload = start_generate_dbt_checkpoint_a_job(
            report_path=report_path,
            glossary_path=glossary_path,
            target_dialect_raw=body.target_dialect,
            dbt_models_dir=output_dir,
            dbt_project_dir=dbt_project_dir,
            checkpoint_dir=checkpoint_dir,
            dlq_dir=dlq_dir,
            bypass_wave=body.bypass_wave,
            wave_id_filter=body.wave_id_filter,
            stop_on_first_error=body.stop_on_first_error,
            approve_checkpoint_a=body.approve_checkpoint_a,
            run_execution=body.run_execution,
        )
        return {"job_id": job_id, "job": job_payload}
    except Exception as exc:
        logger.exception("checkpoint-a start failed")
        raise HTTPException(status_code=500, detail=f"Checkpoint-A start failed: {exc}") from exc


@router.get("/checkpoint-a/job/{job_id}")
def cockpit_poll_checkpoint_a(job_id: str) -> dict[str, Any]:
    """Poll checkpoint-A async job state and optional checkpoint payload."""
    checkpoint_dir = deps.get_checkpoint_dir()
    try:
        job, checkpoint = poll_generate_dbt_checkpoint_a_job(checkpoint_dir=checkpoint_dir, job_id=job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "job": job,
            "checkpoint_a": checkpoint.model_dump(mode="json") if checkpoint is not None else None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("checkpoint-a poll failed")
        raise HTTPException(status_code=500, detail=f"Checkpoint-A poll failed: {exc}") from exc


@router.post("/model/risk")
def cockpit_model_risk(body: ModelRiskRequest) -> dict[str, Any]:
    """Analyze model risk/scenarios through cockpit agent wrappers."""
    try:
        return analyze_model_risk_and_scenarios(
            checkpoint_dir=deps.get_checkpoint_dir(),
            model_name=body.model_name,
            sql=body.sql,
        )
    except Exception as exc:
        logger.exception("model risk analysis failed")
        raise HTTPException(status_code=500, detail=f"Model risk analysis failed: {exc}") from exc


@router.post("/model/synthetic")
def cockpit_model_synthetic(body: SyntheticDataRequest) -> dict[str, Any]:
    """Generate synthetic model data by delegating to existing cockpit service function."""
    try:
        rc, message, path = generate_synthetic_data_for_model(
            checkpoint_dir=deps.get_checkpoint_dir(),
            model_name=body.model_name,
            schema_columns=body.schema_columns,
            approved=body.approved,
            row_cap=body.row_cap,
        )
        return {"rc": rc, "message": message, "path": path}
    except Exception as exc:
        logger.exception("synthetic generation failed")
        raise HTTPException(status_code=500, detail=f"Synthetic generation failed: {exc}") from exc


@router.post("/model/sql-patch")
def cockpit_sql_patch(body: SqlPatchRequest) -> dict[str, str]:
    """Propose SQL patch from conversational question over current model SQL."""
    try:
        return propose_sql_patch_from_chat(
            checkpoint_dir=deps.get_checkpoint_dir(),
            model_name=body.model_name,
            sql=body.sql,
            question=body.question,
        )
    except Exception as exc:
        logger.exception("sql patch proposal failed")
        raise HTTPException(status_code=500, detail=f"SQL patch proposal failed: {exc}") from exc


@router.post("/wave/stress")
def cockpit_wave_stress(body: WaveStressRequest) -> dict[str, Any]:
    """Run wave stress-test summarization on provided model statuses."""
    try:
        return run_wave_stress_test(
            checkpoint_dir=deps.get_checkpoint_dir(),
            wave_id=body.wave_id,
            model_names=body.model_names,
            model_states=body.model_states,
        )
    except Exception as exc:
        logger.exception("wave stress failed")
        raise HTTPException(status_code=500, detail=f"Wave stress failed: {exc}") from exc


@router.post("/checkpoint-b/apply-fix")
def cockpit_apply_fix(body: ApplyFixRequest) -> dict[str, Any]:
    """Apply AI SQL fix to checkpoint-B model through existing runner workflow."""
    try:
        rc, message = apply_ai_fix_from_checkpoint(
            dbt_project_dir=deps.get_dbt_project_dir(),
            checkpoint_dir=deps.get_checkpoint_dir(),
            model_name=body.model_name,
            ai_sql=body.ai_sql,
        )
        return {"rc": rc, "message": message}
    except Exception as exc:
        logger.exception("checkpoint-b apply fix failed")
        raise HTTPException(status_code=500, detail=f"Checkpoint-B apply fix failed: {exc}") from exc


@router.post("/checkpoint-b/reject")
def cockpit_reject_checkpoint(body: RejectCheckpointRequest) -> dict[str, Any]:
    """Reject checkpoint-B artifact and move it to DLQ via existing runner helper."""
    try:
        rc, message = reject_checkpoint_b_to_dlq(
            model_name=body.model_name,
            checkpoint_dir=deps.get_checkpoint_dir(),
            dlq_dir=deps.get_dlq_dir(),
        )
        return {"rc": rc, "message": message}
    except Exception as exc:
        logger.exception("checkpoint-b reject failed")
        raise HTTPException(status_code=500, detail=f"Checkpoint-B reject failed: {exc}") from exc

