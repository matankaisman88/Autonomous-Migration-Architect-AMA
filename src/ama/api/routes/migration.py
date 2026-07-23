from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ama.api import deps
from ama.dbt_migration.generator import normalize_candidate_sql
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
    report_path = deps.get_report_path(report_id)
    dbt_project_dir = deps.get_dbt_project_dir()
    output_dir = deps.get_output_dir(report_id)
    normalized_sql = normalize_candidate_sql(body.sql, body.table_key)
    if not normalized_sql:
        raise HTTPException(status_code=400, detail="approved SQL is invalid or unsafe for target table")
    original_sql = normalized_sql

    def _test_model() -> dict[str, Any]:
        return agent_tools.test_model(
            dbt_project_dir=dbt_project_dir,
            model_name=body.model_name,
            target=None,
            report=report,
            report_path=report_path,
            primary_table_key=body.table_key,
            schema_yml=body.schema_yml,
        )

    try:
        sql_path, _ = _write_model_files(
            output_dir=output_dir,
            model_name=body.model_name,
            sql=normalized_sql,
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

    # --- Phase 1 diagnostics (capture-only; does NOT change fallback behavior) ---
    # These record the dbt failure output at each stage so it is no longer discarded
    # the moment the stage-3 passthrough happens to pass.
    stage1_error: str | None = None  # proposed SQL (body.sql) failed dbt test
    stage2_error: str | None = None  # LLM apply_fix result failed dbt test (or produced no SQL)
    passthrough_used = False  # stage-3 `SELECT * FROM <table>` fallback was written & tested

    try:
        test_res = _test_model()
        test_passed = bool(test_res.get("success"))
        if not test_passed:
            # Stage 1 failed: capture the proposed-SQL failure unconditionally.
            stage1_error = str(test_res.get("logs") or test_res.get("reason") or "dbt test failed")
            fix_attempted = True
            fix_res = agent_tools.apply_fix(
                dbt_project_dir=dbt_project_dir,
                model_name=body.model_name,
                error_log=str(test_res.get("logs") or test_res.get("reason") or ""),
                attempt_history=[],
            )
            raw_fix_sql = str(fix_res.get("corrected_sql") or "")
            fix_sql = normalize_candidate_sql(raw_fix_sql, body.table_key) or None
            if fix_sql:
                _write_model_files(
                    output_dir=output_dir,
                    model_name=body.model_name,
                    sql=fix_sql,
                    schema_yml=body.schema_yml,
                )
                retry = _test_model()
                test_passed = bool(retry.get("success"))
                if not test_passed:
                    # Stage 2 failed: capture the post-LLM-fix failure unconditionally,
                    # before the stage-3 passthrough can overwrite/hide it.
                    stage2_error = str(retry.get("logs") or retry.get("reason") or "dbt test failed")
                    # Last-resort compatibility path: source schema may differ from mapped DDL names.
                    passthrough_sql = normalize_candidate_sql(f"SELECT * FROM {body.table_key}", body.table_key)
                    if passthrough_sql:
                        passthrough_used = True
                        # Structured server-side signal so we can correlate which tables
                        # hit the silent degradation and why (stage1/stage2 root cause).
                        logger.warning(
                            "migration_approve stage3 passthrough fallback reached: "
                            "report_id=%s model_name=%s table_key=%s dbt_target=%s "
                            "approved_by=%s | stage1_error=%s | stage2_error=%s",
                            report_id,
                            body.model_name,
                            body.table_key,
                            "dbt-project-default (test_model target=None)",
                            body.approved_by,
                            (stage1_error or "")[:2000],
                            (stage2_error or "")[:2000],
                        )
                        _write_model_files(
                            output_dir=output_dir,
                            model_name=body.model_name,
                            sql=passthrough_sql,
                            schema_yml=body.schema_yml,
                        )
                        retry2 = _test_model()
                        test_passed = bool(retry2.get("success"))
                        if test_passed:
                            fix_sql = passthrough_sql
                        else:
                            error = str(retry2.get("logs") or retry2.get("reason") or "dbt test failed")
                    else:
                        error = str(retry.get("logs") or retry.get("reason") or "dbt test failed")
            else:
                # LLM produced no usable fix SQL — record that as the stage-2 outcome.
                stage2_error = "apply_fix returned no usable corrected_sql"
                error = str(test_res.get("logs") or test_res.get("reason") or "dbt test failed")
    except Exception as exc:
        logger.exception("approve flow failed")
        raise HTTPException(status_code=500, detail=f"Approval flow failed: {exc}") from exc

    final_sql = fix_sql if fix_sql else original_sql

    if passthrough_used and error is None:
        status = "degraded_passthrough"
        success = False
        test_passed_out = False
    elif error is None:
        status = "approved"
        success = True
        test_passed_out = True
    else:
        status = "failed"
        success = False
        test_passed_out = False

    if status == "approved":
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

    payload: dict[str, Any] = {
        "status": status,
        "success": success,
        "model_name": body.model_name,
        "sql_path": str(sql_path),
        "test_passed": test_passed_out,
        "fix_attempted": fix_attempted,
        "fix_sql": fix_sql,
        "error": error,
        "stage1_error": stage1_error,
        "stage2_error": stage2_error,
        "passthrough_used": passthrough_used,
    }
    if final_sql.strip() != original_sql.strip():
        payload["original_sql"] = original_sql
        payload["final_sql"] = final_sql
    return payload

