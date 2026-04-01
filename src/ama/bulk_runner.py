from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ama.dbt_migration.generator import normalize_candidate_sql
from ama.dbt_migration.writer import _write_model_files
from ama.hitl_apply import decision_from_queue
from ama.migration_agent import agent_tools as migration_agent_tools
from ama.scale_engine.audit import append_decision

_BULK_JOBS_LOCK = threading.Lock()
_BULK_JOBS: dict[str, dict[str, Any]] = {}


def _bulk_jobs_dir(dbt_project_dir: Path) -> Path:
    return (dbt_project_dir / "target" / "bulk_jobs").resolve()


def _bulk_job_path(*, dbt_project_dir: Path, job_id: str) -> Path:
    return _bulk_jobs_dir(dbt_project_dir) / f"{job_id}.json"


def _bulk_job_write(*, dbt_project_dir: Path, job_id: str, payload: dict[str, Any]) -> None:
    try:
        out_dir = _bulk_jobs_dir(dbt_project_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        _bulk_job_path(dbt_project_dir=dbt_project_dir, job_id=job_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _bulk_job_load(*, dbt_project_dir: Path, job_id: str) -> dict[str, Any] | None:
    p = _bulk_job_path(dbt_project_dir=dbt_project_dir, job_id=job_id)
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _bulk_job_clear(*, dbt_project_dir: Path, job_id: str) -> None:
    with _BULK_JOBS_LOCK:
        _BULK_JOBS.pop(job_id, None)
    try:
        _bulk_job_path(dbt_project_dir=dbt_project_dir, job_id=job_id).unlink(missing_ok=True)
    except OSError:
        pass


def _run_bulk_job(
    *,
    job_id: str,
    table_keys: list[str],
    report: dict[str, Any],
    report_path: Path,
    dialect: str,
    dbt_project_dir: Path,
    output_dir: Path,
    contract_id: str,
    scored_rows: dict[str, Any],
    max_workers: int = 4,
    dbt_workers: int = 1,
    dbt_target: str | None = None,
) -> None:
    total = len(table_keys)
    try:
        with _BULK_JOBS_LOCK:
            if job_id not in _BULK_JOBS:
                _BULK_JOBS[job_id] = {
                    "status": "queued",
                    "total": total,
                    "completed": 0,
                    "current_table": "",
                    "success": [],
                    "failed": [],
                    "error": "",
                    "workers": max(1, int(max_workers or 1)),
                    "dbt_workers": max(1, int(dbt_workers or 1)),
                }
            _BULK_JOBS[job_id]["status"] = "running"
            _BULK_JOBS[job_id]["total"] = total
            _BULK_JOBS[job_id]["workers"] = max(1, int(max_workers or 1))
            _BULK_JOBS[job_id]["dbt_workers"] = max(1, int(dbt_workers or 1))
            _bulk_job_write(
                dbt_project_dir=dbt_project_dir,
                job_id=job_id,
                payload=dict(_BULK_JOBS[job_id]),
            )

        prepared_models: dict[str, str] = {}

        def _process_one(table_key: str) -> tuple[str, bool, str, str]:
            try:
                prop = migration_agent_tools.propose_dbt_model(
                    report=report,
                    report_path=report_path,
                    table=table_key,
                    dialect=dialect,
                    glossary_path=None,
                )
                model_name = str(prop.get("model_name") or table_key.replace(".", "_"))
                raw_sql = str(prop.get("sql") or "")
                sql = normalize_candidate_sql(raw_sql, table_key)
                schema_yml = str(prop.get("schema_yml") or "")
                _write_model_files(
                    output_dir=output_dir,
                    model_name=model_name,
                    sql=sql,
                    schema_yml=schema_yml,
                )
                ok = bool(sql.strip())
                row = scored_rows.get(table_key)
                if row is not None:
                    append_decision(
                        table_key=table_key,
                        decision=decision_from_queue(str(row.get("queue") or "green")),
                        confidence=row["confidence_result"],
                        criticality=row["criticality_result"],
                        anomaly_flags=row["anomaly_flags"],
                        contract_id=contract_id,
                        approved_by="dashboard",
                        approved_at=datetime.now(timezone.utc).isoformat(),
                    )
                return table_key, ok, model_name, ""
            except Exception:
                return table_key, False, "", "prepare/write exception"

        workers = max(1, min(int(max_workers or 4), 8))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process_one, tk): tk for tk in table_keys}
            done_count = 0
            for fut in as_completed(futs):
                table_key = futs[fut]
                with _BULK_JOBS_LOCK:
                    _BULK_JOBS[job_id]["current_table"] = table_key
                tk, ok, model_name, reason = fut.result()
                with _BULK_JOBS_LOCK:
                    if not ok:
                        _BULK_JOBS[job_id]["failed"].append({"table_key": tk, "reason": reason})
                    else:
                        prepared_models[tk] = model_name
                    done_count += 1
                    _BULK_JOBS[job_id]["completed"] = done_count
                    _bulk_job_write(
                        dbt_project_dir=dbt_project_dir,
                        job_id=job_id,
                        payload=dict(_BULK_JOBS[job_id]),
                    )
        # Batch dbt validation for prepared models to reduce process-start overhead on large bulks.
        if prepared_models:
            with _BULK_JOBS_LOCK:
                _BULK_JOBS[job_id]["status"] = "running"
                _BULK_JOBS[job_id]["current_table"] = "batched dbt validation"
                _BULK_JOBS[job_id]["completed"] = 0
                _BULK_JOBS[job_id]["total"] = len(prepared_models)
                _bulk_job_write(
                    dbt_project_dir=dbt_project_dir,
                    job_id=job_id,
                    payload=dict(_BULK_JOBS[job_id]),
                )
            model_results = migration_agent_tools.test_models_batch(
                dbt_project_dir=dbt_project_dir,
                model_names=list(prepared_models.values()),
                target=dbt_target,
                chunk_size=50,
            )
            done_count = 0
            for table_key, model_name in prepared_models.items():
                r = model_results.get(model_name) or {}
                ok = bool(r.get("success"))
                reason = str(r.get("reason") or "").strip()
                if not ok:
                    # Keep parity with single-table execution: one auto-fix pass per failed model.
                    fix = migration_agent_tools.apply_fix(
                        dbt_project_dir=dbt_project_dir,
                        model_name=model_name,
                        error_log=reason,
                        attempt_history=[],
                    )
                    raw_corrected_sql = str((fix or {}).get("corrected_sql") or "").strip()
                    corrected_sql = normalize_candidate_sql(raw_corrected_sql, table_key)
                    if corrected_sql:
                        try:
                            schema_path = output_dir / f"{model_name}.schema.yml"
                            schema_yml = schema_path.read_text(encoding="utf-8") if schema_path.is_file() else ""
                        except OSError:
                            schema_yml = ""
                        _write_model_files(
                            output_dir=output_dir,
                            model_name=model_name,
                            sql=corrected_sql,
                            schema_yml=schema_yml,
                        )
                        retry = migration_agent_tools.test_model(
                            dbt_project_dir=dbt_project_dir,
                            model_name=model_name,
                            target=dbt_target,
                        )
                        ok = bool(retry.get("success"))
                        reason = str(retry.get("logs") or reason).strip()
                with _BULK_JOBS_LOCK:
                    if ok:
                        _BULK_JOBS[job_id]["success"].append(table_key)
                    else:
                        _BULK_JOBS[job_id]["failed"].append({"table_key": table_key, "reason": reason[:600]})
                    done_count += 1
                    _BULK_JOBS[job_id]["completed"] = done_count
                    _BULK_JOBS[job_id]["current_table"] = table_key
                    _bulk_job_write(
                        dbt_project_dir=dbt_project_dir,
                        job_id=job_id,
                        payload=dict(_BULK_JOBS[job_id]),
                    )
        with _BULK_JOBS_LOCK:
            _BULK_JOBS[job_id]["status"] = "done"
            _bulk_job_write(
                dbt_project_dir=dbt_project_dir,
                job_id=job_id,
                payload=dict(_BULK_JOBS[job_id]),
            )
    except Exception as exc:
        with _BULK_JOBS_LOCK:
            _BULK_JOBS[job_id]["status"] = "failed"
            _BULK_JOBS[job_id]["error"] = str(exc)
            _bulk_job_write(
                dbt_project_dir=dbt_project_dir,
                job_id=job_id,
                payload=dict(_BULK_JOBS[job_id]),
            )
