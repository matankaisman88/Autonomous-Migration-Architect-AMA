from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ama.api import deps
from ama.dbt_migration.writer import _write_model_files
from ama.hitl_apply import decision_from_queue
from ama.migration_agent import agent_tools
from ama.scale_engine.audit import append_decision

logger = logging.getLogger(__name__)
router = APIRouter()

_ALLOWED_DIALECTS = {"duckdb", "snowflake", "bigquery", "redshift"}
_PROPOSAL_CACHE_LOCK = threading.Lock()
_PROPOSAL_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


class ProposeRequest(BaseModel):
    table_key: str
    dialect: str = "duckdb"


class ApproveRequest(BaseModel):
    model_name: str
    sql: str
    schema_yml: str
    table_key: str
    approved_by: str = "api"


@router.post("/{report_id}/propose")
def migration_propose(report_id: str, body: ProposeRequest) -> dict[str, Any]:
    """Call propose_dbt_model directly and return generated SQL artifacts."""
    report = deps.get_report(report_id)
    report_path = deps.get_report_path(report_id)
    dialect = body.dialect.lower().strip()
    table_key = str(body.table_key).strip()
    if dialect not in _ALLOWED_DIALECTS:
        raise HTTPException(status_code=400, detail="invalid dialect")
    if not table_key:
        raise HTTPException(status_code=400, detail="table_key is required")
    cache_key = (report_id, table_key, dialect)
    with _PROPOSAL_CACHE_LOCK:
        cached = _PROPOSAL_CACHE.get(cache_key)
    if isinstance(cached, dict):
        cached_response = dict(cached)
        cached_response["cached"] = True
        return cached_response
    started_at = time.perf_counter()
    try:
        res = agent_tools.propose_dbt_model(
            report=report,
            report_path=report_path,
            table=table_key,
            dialect=dialect,
            glossary_path=None,
        )
    except Exception as exc:
        logger.exception("propose failed")
        raise HTTPException(status_code=500, detail=f"Propose failed: {exc}") from exc
    response = {
        "model_name": str(res.get("model_name") or ""),
        "sql": str(res.get("sql") or ""),
        "schema_yml": str(res.get("schema_yml") or ""),
        "generation_confidence": float(res.get("generation_confidence") or 0.0),
        "mapping_rows": res.get("mapping_rows") if isinstance(res.get("mapping_rows"), list) else [],
        "response_ms": int((time.perf_counter() - started_at) * 1000),
        "cached": False,
    }
    with _PROPOSAL_CACHE_LOCK:
        _PROPOSAL_CACHE[cache_key] = dict(response)
    return response


@router.post("/{report_id}/approve")
def migration_approve(report_id: str, body: ApproveRequest) -> dict[str, Any]:
    """Write approved model files, run test_model, one fix pass, and append audit decision."""
    report = deps.get_report(report_id)
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)

    try:
        sql_path, _ = _write_model_files(
            output_dir=output_dir,
            model_name=body.model_name,
            sql=body.sql,
            schema_yml=body.schema_yml,
        )
    except Exception as exc:
        logger.exception("write model failed")
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc
    with _PROPOSAL_CACHE_LOCK:
        _PROPOSAL_CACHE.pop((report_id, str(body.table_key).strip(), "duckdb"), None)
        _PROPOSAL_CACHE.pop((report_id, str(body.table_key).strip(), "snowflake"), None)
        _PROPOSAL_CACHE.pop((report_id, str(body.table_key).strip(), "bigquery"), None)
        _PROPOSAL_CACHE.pop((report_id, str(body.table_key).strip(), "redshift"), None)

    fix_attempted = False
    fix_sql: str | None = None
    error: str | None = None

    try:
        test_res = agent_tools.test_model(dbt_project_dir=dbt_project_dir, model_name=body.model_name, target=None)
        test_passed = bool(test_res.get("success"))
        if not test_passed:
            fix_attempted = True
            fix_res = agent_tools.apply_fix(
                dbt_project_dir=dbt_project_dir,
                model_name=body.model_name,
                error_log=str(test_res.get("logs") or test_res.get("reason") or ""),
                attempt_history=[],
            )
            fix_sql = str(fix_res.get("corrected_sql") or "") or None
            if fix_sql:
                _write_model_files(
                    output_dir=output_dir,
                    model_name=body.model_name,
                    sql=fix_sql,
                    schema_yml=body.schema_yml,
                )
                retry = agent_tools.test_model(dbt_project_dir=dbt_project_dir, model_name=body.model_name, target=None)
                test_passed = bool(retry.get("success"))
                if not test_passed:
                    error = str(retry.get("logs") or retry.get("reason") or "dbt test failed")
            else:
                error = str(test_res.get("logs") or test_res.get("reason") or "dbt test failed")
    except Exception as exc:
        logger.exception("approve flow failed")
        raise HTTPException(status_code=500, detail=f"Approval flow failed: {exc}") from exc

    try:
        eval_res = agent_tools.explain_table_score(report=report, table_key=body.table_key)
        append_decision(
            table_key=body.table_key,
            decision="bulk_approved",
            confidence=eval_res.confidence,
            criticality=eval_res.criticality,
            anomaly_flags=eval_res.anomaly_flags,
            contract_id="manual",
            approved_by=body.approved_by,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        logger.exception("append_decision failed")

    return {
        "success": bool(error is None),
        "model_name": body.model_name,
        "sql_path": str(sql_path),
        "test_passed": bool(error is None),
        "fix_attempted": fix_attempted,
        "fix_sql": fix_sql,
        "error": error,
    }

