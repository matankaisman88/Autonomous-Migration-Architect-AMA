from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ama.api import deps
from ama.dbt_migration.service import start_generate_dbt_checkpoint_a_job
from ama.dbt_migration.writer import _write_model_files
from ama.migration_agent import agent_tools
from ama.migration_agent.engine import run_agent_turn

logger = logging.getLogger(__name__)
router = APIRouter()


class AgentTurnRequest(BaseModel):
    user_message: str
    state: dict[str, Any]
    dialect: str = "duckdb"
    pending_write_action: str | None = None
    pending_write_sql: str | None = None
    pending_write_schema_yml: str | None = None


def _append_chat(state: dict[str, Any], role: str, content: str) -> None:
    msgs = state.get("messages")
    if not isinstance(msgs, list):
        msgs = []
        state["messages"] = msgs
    msgs.append({"role": role, "content": content})


def _handle_legacy_agent_command(report_id: str, body: AgentTurnRequest) -> dict[str, Any] | None:
    user_message = str(body.user_message or "").strip()
    state = dict(body.state or {})
    migrate_wave = re.match(r"^migrate\s+wave\s+(\d+)$", user_message, flags=re.IGNORECASE)
    if not migrate_wave:
        return None

    wave_id = int(migrate_wave.group(1))
    report_path = deps.get_report_path(report_id)
    checkpoint_dir = deps.get_checkpoint_dir()
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)
    dlq_dir = deps.get_dlq_dir()
    glossary_path = report_path.parent / "glossary.json"
    if not glossary_path.is_file():
        glossary_path = None

    job_id, job_payload = start_generate_dbt_checkpoint_a_job(
        report_path=report_path,
        glossary_path=glossary_path,
        target_dialect_raw=body.dialect or "duckdb",
        dbt_models_dir=output_dir,
        dbt_project_dir=dbt_project_dir,
        checkpoint_dir=checkpoint_dir,
        dlq_dir=dlq_dir,
        bypass_wave=None,
        wave_id_filter=wave_id,
        stop_on_first_error=False,
        approve_checkpoint_a=False,
        run_execution=False,
    )
    _append_chat(state, "user", user_message)
    _append_chat(
        state,
        "assistant",
        f"Started Checkpoint-A generation for wave {wave_id}. Job ID: {job_id}. Track progress in DBT Cockpit.",
    )
    state["selected_wave_id"] = wave_id
    state["checkpoint_a_job_id"] = job_id
    return {
        "status": "accepted",
        "message": f"Started migration wave {wave_id}.",
        "state": state,
        "pending_write": None,
        "tokens_used": 0,
        "cost_est": 0.0,
        "job_id": job_id,
        "job": job_payload,
    }


@router.post("/{report_id}/turn")
def agent_turn(report_id: str, body: AgentTurnRequest) -> dict[str, Any]:
    """Run one stateless migration-agent turn; client owns and resubmits full state."""
    report = deps.get_report(report_id)
    report_path = deps.get_report_path(report_id)
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)
    try:
        legacy = _handle_legacy_agent_command(report_id, body)
        if legacy is not None:
            return legacy
    except Exception as exc:
        logger.exception("legacy agent command failed")
        raise HTTPException(status_code=500, detail=f"Legacy command failed: {exc}") from exc
    action = str(body.pending_write_action or "").strip().lower()
    if action in {"approve", "reject"}:
        state = dict(body.state or {})
        pending = state.get("pending_write")
        if not isinstance(pending, dict):
            raise HTTPException(status_code=400, detail="No pending_write payload in state")
        if action == "reject":
            state["pending_write"] = None
            _append_chat(state, "assistant", "Pending write rejected by user.")
            return {
                "status": "rejected",
                "message": "Pending write rejected.",
                "state": state,
                "pending_write": None,
                "tokens_used": 0,
                "cost_est": 0.0,
            }
        model_name = str(pending.get("model_name") or "").strip()
        if not model_name:
            raise HTTPException(status_code=400, detail="pending_write.model_name is required")
        sql = str(body.pending_write_sql if body.pending_write_sql is not None else pending.get("sql") or "")
        schema_yml = str(
            body.pending_write_schema_yml
            if body.pending_write_schema_yml is not None
            else pending.get("schema_yml") or ""
        )
        try:
            _write_model_files(
                output_dir=output_dir,
                model_name=model_name,
                sql=sql,
                schema_yml=schema_yml,
            )
            test_result = agent_tools.test_model(
                dbt_project_dir=dbt_project_dir,
                model_name=model_name,
                target=None,
            )
            state["pending_write"] = None
            _append_chat(
                state,
                "assistant",
                f"Approved and wrote model `{model_name}`. Test success: {bool(test_result.get('success'))}.",
            )
            return {
                "status": "approved",
                "message": f"Model {model_name} written to disk.",
                "state": state,
                "pending_write": None,
                "tokens_used": 0,
                "cost_est": 0.0,
                "test_result": test_result,
            }
        except Exception as exc:
            logger.exception("pending write approval failed")
            raise HTTPException(status_code=500, detail=f"Pending write approval failed: {exc}") from exc
    try:
        result = run_agent_turn(
            state=body.state,
            report=report,
            report_path=report_path,
            dbt_project_dir=dbt_project_dir,
            output_dir=output_dir,
            glossary_path=None,
            user_message=body.user_message,
            default_dialect=body.dialect,
        )
    except Exception as exc:
        logger.exception("agent turn failed")
        raise HTTPException(status_code=500, detail=f"Agent turn failed: {exc}") from exc
    return {
        "status": result.status,
        "message": result.message,
        "state": result.state,
        "pending_write": result.pending_write,
        "tokens_used": result.tokens_used,
        "cost_est": result.cost_est,
    }

