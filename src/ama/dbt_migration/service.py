from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable
from functools import lru_cache

from ama.dbt_migration.generator import generate_model_artifact
from ama.dbt_migration.cockpit_agents import (
    chat_model_agent,
    data_gen_agent,
    risk_agent,
    scenario_agent,
    wave_summary_agent,
)
from ama.dbt_migration.models import CheckpointAArtifact, MigrationSessionState, MigrationStatus
from ama.dbt_migration.runner import approve_checkpoint_b_sql, execute_models_with_fix_loop
from ama.planner import AutonomousPlanner
from ama.dbt_migration.sql_transpile import validate_target_dialect
from ama.dbt_migration.writer import write_model_artifacts
from ama.dbt_migration.models import GenerationJobArtifact, GenerationJobStatus, JobProgressEvent

logger = logging.getLogger(__name__)


def _jobs_dir(checkpoint_dir: Path) -> Path:
    return checkpoint_dir / "jobs"


def _job_path(checkpoint_dir: Path, job_id: str) -> Path:
    return _jobs_dir(checkpoint_dir) / f"{job_id}.json"


def _events_path(checkpoint_dir: Path, job_id: str) -> Path:
    return _jobs_dir(checkpoint_dir) / f"{job_id}.events.jsonl"


def load_job(checkpoint_dir: Path, job_id: str) -> dict[str, Any]:
    """
    Load job artifact as a dict for forward-compatibility.
    """
    path = _job_path(checkpoint_dir, job_id)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_job(checkpoint_dir: Path, job_id: str, payload: dict[str, Any]) -> Path:
    _jobs_dir(checkpoint_dir).mkdir(parents=True, exist_ok=True)
    path = _job_path(checkpoint_dir, job_id)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def append_event(checkpoint_dir: Path, job_id: str, event: dict[str, Any]) -> Path:
    """
    Append one JSON event line (JSONL) for UI progress.
    """
    _jobs_dir(checkpoint_dir).mkdir(parents=True, exist_ok=True)
    path = _events_path(checkpoint_dir, job_id)
    # Best-effort append; malformed lines can be skipped by readers.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def _checkpoint_a_path(checkpoint_dir: Path, job_id: str) -> Path:
    return _jobs_dir(checkpoint_dir) / f"{job_id}.checkpoint_a.json"


def save_checkpoint_a_for_job(checkpoint_dir: Path, job_id: str, checkpoint_a: CheckpointAArtifact) -> Path:
    """
    Persist the full CheckpointAArtifact so the UI can load it asynchronously.
    """
    _jobs_dir(checkpoint_dir).mkdir(parents=True, exist_ok=True)
    path = _checkpoint_a_path(checkpoint_dir, job_id)
    payload = checkpoint_a.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_checkpoint_a_for_job(checkpoint_dir: Path, job_id: str) -> CheckpointAArtifact | None:
    path = _checkpoint_a_path(checkpoint_dir, job_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return CheckpointAArtifact.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_generate_dbt_checkpoint_a_job(
    *,
    report_path: Path,
    glossary_path: Path | None,
    target_dialect_raw: str,
    dbt_models_dir: Path,
    dbt_project_dir: Path,
    checkpoint_dir: Path,
    dlq_dir: Path,
    bypass_wave: int | None,
    wave_id_filter: int | None,
    stop_on_first_error: bool,
    approve_checkpoint_a: bool,
    run_execution: bool,
) -> tuple[str, dict[str, Any]]:
    """
    Start async Checkpoint A generation.

    Returns (job_id, initial_job_payload).
    """
    import uuid

    job_id = str(uuid.uuid4())
    jobs_payload: dict[str, Any] = GenerationJobArtifact(
        job_id=job_id,
        status=GenerationJobStatus.RUNNING,
        created_at=_utc_now_iso(),
        updated_at=_utc_now_iso(),
        total_models=0,
        completed_models=0,
        failed_models=0,
        report_path=str(report_path),
        glossary_path=str(glossary_path) if glossary_path else "",
        target_dialect=str(target_dialect_raw),
        dbt_models_dir=str(dbt_models_dir),
        dbt_project_dir=str(dbt_project_dir),
        checkpoint_dir=str(checkpoint_dir),
    ).model_dump(mode="json")

    save_job(checkpoint_dir, job_id, jobs_payload)

    def _progress(event_type: str, payload: dict[str, Any]) -> None:
        # Update job counters opportunistically.
        job = load_job(checkpoint_dir, job_id)
        if not job:
            return
        updated = False
        if event_type == "JOB_TOTAL":
            job["total_models"] = int(payload.get("total_models") or 0)
            updated = True
        elif event_type == "MODEL_DONE":
            job["completed_models"] = int(job.get("completed_models") or 0) + 1
            updated = True
        elif event_type in {"MODEL_FAILED"}:
            job["failed_models"] = int(job.get("failed_models") or 0) + 1
            updated = True
        if updated:
            job["updated_at"] = _utc_now_iso()
            save_job(checkpoint_dir, job_id, job)

        append_event(checkpoint_dir, job_id, {"event_type": event_type, "timestamp": _utc_now_iso(), **payload})

    def _worker() -> None:
        try:
            # Keep exact orchestration behavior as run_generate_dbt.
            _state, checkpoint_a, _written = run_generate_dbt(
                report_path=report_path,
                glossary_path=glossary_path,
                target_dialect_raw=target_dialect_raw,
                dbt_models_dir=dbt_models_dir,
                dbt_project_dir=dbt_project_dir,
                checkpoint_dir=checkpoint_dir,
                dlq_dir=dlq_dir,
                bypass_wave=bypass_wave,
                wave_id_filter=wave_id_filter,
                stop_on_first_error=stop_on_first_error,
                approve_checkpoint_a=approve_checkpoint_a,
                run_execution=run_execution,
                progress_callback=_progress,
                job_id=job_id,
            )
            save_checkpoint_a_for_job(checkpoint_dir, job_id, checkpoint_a)
            job = load_job(checkpoint_dir, job_id)
            job["status"] = GenerationJobStatus.SUCCESS
            job["updated_at"] = _utc_now_iso()
            job["checkpoint_a_path"] = str(_checkpoint_a_path(checkpoint_dir, job_id))
            save_job(checkpoint_dir, job_id, job)
        except Exception as exc:  # pragma: no cover
            job = load_job(checkpoint_dir, job_id)
            job["status"] = GenerationJobStatus.FAILED
            job["updated_at"] = _utc_now_iso()
            job["error"] = str(exc)
            save_job(checkpoint_dir, job_id, job)
            logger.exception("checkpoint_a_job_failed", extra={"job_id": job_id, "error": str(exc)})

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return job_id, jobs_payload


def poll_generate_dbt_checkpoint_a_job(
    *,
    checkpoint_dir: Path,
    job_id: str,
) -> tuple[dict[str, Any], CheckpointAArtifact | None]:
    job = load_job(checkpoint_dir, job_id)
    if not job:
        return {}, None
    status = str(job.get("status") or "")
    if status == GenerationJobStatus.SUCCESS:
        checkpoint_a = load_checkpoint_a_for_job(checkpoint_dir, job_id)
        return job, checkpoint_a
    return job, None


def _execute_approved_checkpoint_models(
    *,
    checkpoint: CheckpointAArtifact,
    report_path: Path,
    dbt_project_dir: Path,
    checkpoint_dir: Path,
    dlq_dir: Path,
    bypass_wave: int | None,
    stop_on_first_error: bool,
    max_fix_attempts: int = 3,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    table_to_model = {a.table_key: a.model_name for a in checkpoint.generated_models}
    wave_to_tables = _build_wave_to_tables(report)
    wave_to_model_names: dict[int, list[str]] = {}
    for wave_id, tables in wave_to_tables.items():
        names = [table_to_model[t] for t in tables if t in table_to_model]
        if names:
            wave_to_model_names[wave_id] = names
    if not wave_to_model_names:
        wave_to_model_names = {0: [a.model_name for a in checkpoint.generated_models]}

    model_state, telemetry, blocked = _orchestrate_waves_with_gating(
        wave_to_model_names=wave_to_model_names,
        dbt_project_dir=dbt_project_dir,
        max_fix_attempts=max_fix_attempts,
        dlq_dir=dlq_dir,
        checkpoint_dir=checkpoint_dir,
        bypass_wave=bypass_wave,
        stop_on_first_error=stop_on_first_error,
    )
    failed = [name for name, st in model_state.items() if st in {"HITL_REQUIRED", "FAILED", "REJECTED"}]
    if blocked and failed:
        execution_status = MigrationStatus.HITL_REQUIRED.value
    elif failed:
        execution_status = MigrationStatus.PARTIAL.value
    elif model_state:
        execution_status = MigrationStatus.SUCCESS.value
    else:
        execution_status = MigrationStatus.FAILED.value
    return {
        "execution_status": execution_status,
        "model_state": model_state,
        "review_required": failed,
        "wave_telemetry": telemetry,
        "blocked": blocked,
    }


def approve_checkpoint_a_for_job(
    *,
    checkpoint_dir: Path,
    dlq_dir: Path,
    job_id: str,
    run_execution: bool = False,
    bypass_wave: int | None = None,
    stop_on_first_error: bool = False,
) -> dict[str, Any]:
    """
    Approve a completed Checkpoint-A job: write dbt model files, optionally run dbt in background.
    """
    job = load_job(checkpoint_dir, job_id)
    if not job:
        raise ValueError(f"job not found: {job_id}")
    if str(job.get("status") or "").upper() != GenerationJobStatus.SUCCESS:
        raise ValueError("Checkpoint-A job is not complete yet")
    if bool(job.get("checkpoint_a_approved")):
        raise ValueError("Checkpoint-A already approved for this job")

    checkpoint = load_checkpoint_a_for_job(checkpoint_dir, job_id)
    if checkpoint is None:
        raise ValueError("Checkpoint-A artifact not found")

    dbt_models_dir = Path(str(job.get("dbt_models_dir") or "")).expanduser().resolve()
    dbt_project_dir = Path(str(job.get("dbt_project_dir") or "")).expanduser().resolve()
    report_path = Path(str(job.get("report_path") or "")).expanduser().resolve()
    if not report_path.is_file():
        raise ValueError(f"Report path missing for job: {report_path}")

    written = write_model_artifacts(dbt_models_dir, checkpoint.generated_models)
    job["checkpoint_a_approved"] = True
    job["checkpoint_a_approved_at"] = _utc_now_iso()
    job["written_count"] = len(written)
    job["written_paths"] = [str(p) for p in written]
    save_job(checkpoint_dir, job_id, job)

    result: dict[str, Any] = {
        "job_id": job_id,
        "checkpoint_a_approved": True,
        "written_count": len(written),
        "written_paths": [str(p) for p in written],
        "run_execution": run_execution,
        "review_required_tables": list(checkpoint.review_required_tables),
    }

    if not run_execution:
        return result

    job = load_job(checkpoint_dir, job_id)
    job["execution_status"] = "RUNNING"
    job["execution_started_at"] = _utc_now_iso()
    save_job(checkpoint_dir, job_id, job)
    result["execution_status"] = "RUNNING"

    def _worker() -> None:
        try:
            exec_result = _execute_approved_checkpoint_models(
                checkpoint=checkpoint,
                report_path=report_path,
                dbt_project_dir=dbt_project_dir,
                checkpoint_dir=checkpoint_dir,
                dlq_dir=dlq_dir,
                bypass_wave=bypass_wave,
                stop_on_first_error=stop_on_first_error,
            )
            job_now = load_job(checkpoint_dir, job_id)
            job_now["execution_status"] = exec_result["execution_status"]
            job_now["execution_finished_at"] = _utc_now_iso()
            job_now["execution_model_state"] = exec_result["model_state"]
            job_now["execution_review_required"] = exec_result["review_required"]
            job_now["execution_wave_telemetry"] = exec_result["wave_telemetry"]
            save_job(checkpoint_dir, job_id, job_now)
        except Exception as exc:
            logger.exception("checkpoint_a_execution_failed", extra={"job_id": job_id})
            job_now = load_job(checkpoint_dir, job_id)
            job_now["execution_status"] = "FAILED"
            job_now["execution_finished_at"] = _utc_now_iso()
            job_now["execution_error"] = str(exc)
            save_job(checkpoint_dir, job_id, job_now)

    threading.Thread(target=_worker, name=f"ama-cp-a-exec-{job_id}", daemon=True).start()
    return result


def _insights_path(checkpoint_dir: Path) -> Path:
    return checkpoint_dir / "model_insights.json"


def _load_model_insights(checkpoint_dir: Path) -> dict[str, Any]:
    target = _insights_path(checkpoint_dir)
    mtime = 0.0
    if target.is_file():
        try:
            mtime = target.stat().st_mtime
        except OSError:
            mtime = 0.0
    return _load_model_insights_cached(str(target), mtime)


@lru_cache(maxsize=8)
def _load_model_insights_cached(insights_path: str, mtime: float) -> dict[str, Any]:
    """
    Cache-only read path for `model_insights.json` to reduce repeated JSON parsing.

    `mtime` is part of the cache key to avoid staleness after writes.
    """
    target = Path(insights_path)
    if not target.is_file():
        return {"waves": {}, "models": {}, "telemetry": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"waves": {}, "models": {}, "telemetry": []}
    return payload if isinstance(payload, dict) else {"waves": {}, "models": {}, "telemetry": []}


def _save_model_insights(checkpoint_dir: Path, payload: dict[str, Any]) -> Path:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    target = _insights_path(checkpoint_dir)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def _extract_inventory(report: dict) -> list[dict]:
    discovery = report.get("discovery") if isinstance(report.get("discovery"), dict) else {}
    inventory = discovery.get("inventory")
    if isinstance(inventory, list):
        return [row for row in inventory if isinstance(row, dict)]
    return []


def _extract_broken_tables(report: dict) -> set[str]:
    lineage = report.get("lineage") if isinstance(report.get("lineage"), dict) else {}
    broken = lineage.get("broken_table_keys")
    if isinstance(broken, list):
        return {str(x).strip() for x in broken if str(x).strip()}
    return set()


def _extract_wave_summary(report: dict) -> str:
    return str(
        report.get("migration_context")
        or "Migration wave generated from AMA report"
    )


def _extract_columns_for_table(report: dict, table_key: str) -> list[str]:
    out: list[str] = []
    importance = report.get("importance_ddl")
    if not isinstance(importance, list):
        return out
    for row in importance:
        if not isinstance(row, dict):
            continue
        source_table = str(row.get("source_table") or "").strip()
        if source_table == table_key:
            col = str(row.get("column") or "").strip()
            if col:
                out.append(col)
    return list(dict.fromkeys(out))


def _load_manifest_table_columns(report: dict[str, Any], report_path: Path) -> dict[str, list[str]]:
    alias_merge = report.get("alias_merge")
    if not isinstance(alias_merge, dict):
        return {}
    manifest_path_raw = alias_merge.get("ddl_manifest")
    if not manifest_path_raw:
        return {}
    manifest_path = Path(str(manifest_path_raw)).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (report_path.parent / manifest_path).resolve()
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    out: dict[str, list[str]] = {}
    for table_key, ddl_file_raw in manifest.items():
        if str(table_key).startswith("_"):
            continue
        ddl_file = Path(str(ddl_file_raw)).expanduser()
        if not ddl_file.is_absolute():
            ddl_file = (report_path.parent / ddl_file).resolve()
        if not ddl_file.is_file():
            continue
        try:
            ddl_payload = json.loads(ddl_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        columns = ddl_payload.get("columns") if isinstance(ddl_payload, dict) else None
        if isinstance(columns, list):
            out[str(table_key)] = [str(c).strip() for c in columns if str(c).strip()]
    return out


def render_checkpoint_a_text(checkpoint: CheckpointAArtifact) -> str:
    lines = ["=== CHECKPOINT A ===", f"Wave Summary: {checkpoint.wave_summary}", ""]
    lines.append("Generated SQL:")
    for model in checkpoint.generated_models:
        lines.append(f"\n--- {model.model_name}.sql ---")
        lines.append(model.sql)
    lines.append("\nMapping Table:")
    lines.append("Hebrew Name | English Alias | Source | Warning Flags")
    for row in checkpoint.mapping_rows:
        flags = ",".join(row.warning_flags)
        lines.append(f"{row.hebrew_name} | {row.english_alias} | {row.source.value} | {flags}")
    lines.append("\nREVIEW_REQUIRED:")
    if checkpoint.review_required_tables:
        lines.extend(f"- {t}" for t in checkpoint.review_required_tables)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _build_wave_to_tables(report: dict) -> dict[int, list[str]]:
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=50)
    out: dict[int, list[str]] = {}
    for wave in plan.waves:
        out[int(wave.wave_id)] = [t.full_name for t in wave.tables]
    return out


def _orchestrate_waves_with_gating(
    *,
    wave_to_model_names: dict[int, list[str]],
    dbt_project_dir: Path,
    max_fix_attempts: int,
    dlq_dir: Path,
    checkpoint_dir: Path,
    bypass_wave: int | None,
    stop_on_first_error: bool,
) -> tuple[dict[str, str], list[dict[str, object]], bool]:
    model_state: dict[str, str] = {}
    wave_telemetry: list[dict[str, object]] = []
    blocked = False
    for wave_id in sorted(wave_to_model_names.keys()):
        model_names = wave_to_model_names.get(wave_id) or []
        for model_name in model_names:
            res = execute_models_with_fix_loop(
                dbt_project_dir=dbt_project_dir,
                model_names=[model_name],
                max_attempts=max_fix_attempts,
                dlq_dir=dlq_dir,
                checkpoint_dir=checkpoint_dir,
            )
            st = res.model_results[0].state.value if res.model_results else "FAILED"
            model_state[model_name] = st
            if stop_on_first_error and st in {"HITL_REQUIRED", "FAILED", "REJECTED"}:
                wave_telemetry.append(
                    {
                        "current_wave_id": wave_id,
                        "wave_status": "REVIEW_REQUIRED",
                    }
                )
                return model_state, wave_telemetry, True

        wave_states = [model_state.get(m, "FAILED") for m in model_names]
        ready = all(s in {"SUCCESS", "PARTIAL"} for s in wave_states)
        wave_status = "READY" if ready else "REVIEW_REQUIRED"
        wave_telemetry.append(
            {
                "current_wave_id": wave_id,
                "wave_status": wave_status,
            }
        )
        if not ready:
            if bypass_wave is not None and int(bypass_wave) == int(wave_id):
                logger.warning(
                    "WARNING: Wave %s bypassed with incomplete models. Proceeding to Wave %s.",
                    wave_id,
                    wave_id + 1,
                )
                continue
            blocked = True
            break
    return model_state, wave_telemetry, blocked


def run_generate_dbt(
    *,
    report_path: Path,
    glossary_path: Path | None,
    target_dialect_raw: str,
    dbt_models_dir: Path,
    dbt_project_dir: Path,
    checkpoint_dir: Path,
    dlq_dir: Path,
    bypass_wave: int | None = None,
    stop_on_first_error: bool = False,
    approve_checkpoint_a: bool,
    run_execution: bool,
    wave_id_filter: int | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    job_id: str | None = None,
) -> tuple[MigrationSessionState, CheckpointAArtifact, list[Path]]:
    target_dialect = validate_target_dialect(target_dialect_raw)
    state = MigrationSessionState(target_dialect=target_dialect, status=MigrationStatus.PENDING)
    state.status = MigrationStatus.GENERATING

    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest_columns_by_table = _load_manifest_table_columns(report, report_path)
    glossary = json.loads(glossary_path.read_text(encoding="utf-8")) if glossary_path and glossary_path.is_file() else {}
    if not isinstance(glossary, dict):
        glossary = {}

    inventory = _extract_inventory(report)
    if wave_id_filter is not None:
        allowed_tables: set[str] = set()
        try:
            plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=50)
            for wave in plan.waves:
                try:
                    if int(getattr(wave, "wave_id")) == int(wave_id_filter):
                        allowed_tables = {str(t.full_name) for t in getattr(wave, "tables") or [] if getattr(t, "full_name", None)}
                        break
                except (TypeError, ValueError):
                    continue
        except Exception:
            allowed_tables = set()

        if allowed_tables:
            inventory = [r for r in inventory if str(r.get("full_name") or "").strip() in allowed_tables]
        else:
            # If we can't resolve the wave, fall back to full generation rather than producing an empty checkpoint.
            allowed_tables = set()
    broken_tables = _extract_broken_tables(report)
    alias_registry: dict[str, str] = {}
    artifacts = []
    mapping_rows = []

    if progress_callback is not None:
        try:
            progress_callback("JOB_TOTAL", {"total_models": len(inventory)})
        except Exception:
            pass

    for row in inventory:
        if job_id:
            job = load_job(checkpoint_dir, job_id)
            if str(job.get("status") or "").upper() == GenerationJobStatus.CANCELLED:
                logger.warning("job_cancelled", extra={"job_id": job_id})
                break
        table_key = str(row.get("full_name") or "").strip()
        if not table_key:
            continue
        rationale = str(row.get("rationale") or row.get("reason") or "")
        columns = _extract_columns_for_table(report, table_key)
        if progress_callback is not None:
            try:
                progress_callback("MODEL_START", {"table_key": table_key})
            except Exception:
                pass

        artifact, mapped = generate_model_artifact(
            table_key=table_key,
            raw_columns=columns,
            glossary=glossary,
            alias_registry=alias_registry,
            target_dialect=target_dialect,
            source_ddl_columns=manifest_columns_by_table.get(table_key, []),
            broken=table_key in broken_tables or bool(row.get("is_broken")),
            rationale=rationale,
            thought_callback=progress_callback,
            max_correction_attempts=3,
        )
        artifacts.append(artifact)
        mapping_rows.extend(mapped)

        if progress_callback is not None:
            try:
                progress_callback(
                    "MODEL_DONE",
                    {
                        "model_name": artifact.model_name,
                        "generation_mode": artifact.generation_mode,
                        "generation_confidence": artifact.generation_confidence,
                        "fallback_reason": artifact.fallback_reason,
                    },
                )
            except Exception:
                pass

    review_required = sorted({a.table_key for a in artifacts if a.review_required})
    ai_telemetry: list[dict[str, Any]] = []
    fallback_active = False
    auth_error_detected = False
    rate_limit_detected = False
    for artifact in artifacts:
        ai_telemetry.extend(artifact.ai_telemetry or [])
        if artifact.generation_mode != "ai":
            fallback_active = True
        if artifact.auth_error:
            auth_error_detected = True
        if artifact.rate_limit_error:
            rate_limit_detected = True
    checkpoint = CheckpointAArtifact(
        wave_summary=_extract_wave_summary(report),
        generated_models=artifacts,
        mapping_rows=mapping_rows,
        review_required_tables=review_required,
        ai_telemetry=ai_telemetry,
        fallback_active=fallback_active,
        auth_error_detected=auth_error_detected,
        rate_limit_detected=rate_limit_detected,
    )
    insights_payload = {"waves": {}, "models": {}, "telemetry": ai_telemetry}
    for artifact in artifacts:
        insights_payload["models"][artifact.model_name] = {
            "generation_confidence": artifact.generation_confidence,
            "schema_reasoning": artifact.schema_agent_reasoning,
            "dbt_reasoning": artifact.dbt_agent_reasoning,
            "mapping_decision_tag": artifact.mapping_decision_tag,
            "translation_rationale": artifact.translation_rationale,
            "risk": {},
            "scenarios": [],
            "synthetic_dataset_path": "",
        }
    insights_file = _save_model_insights(checkpoint_dir, insights_payload)
    checkpoint.model_insights = insights_payload
    checkpoint.model_insights_path = str(insights_file)
    state.review_required = review_required
    state.status = MigrationStatus.REVIEW_REQUIRED

    if progress_callback is not None:
        try:
            progress_callback("CHECKPOINT_A_SAVED", {"model_insights_path": str(insights_file)})
        except Exception:
            pass

    if not approve_checkpoint_a:
        return state, checkpoint, []

    state.checkpoint_approved = True
    written = write_model_artifacts(dbt_models_dir, artifacts)
    if not run_execution:
        state.status = MigrationStatus.SUCCESS
        return state, checkpoint, written

    table_to_model = {a.table_key: a.model_name for a in artifacts}
    wave_to_tables = _build_wave_to_tables(report)
    wave_to_model_names: dict[int, list[str]] = {}
    for wave_id, tables in wave_to_tables.items():
        names = [table_to_model[t] for t in tables if t in table_to_model]
        if names:
            wave_to_model_names[wave_id] = names
    if not wave_to_model_names:
        wave_to_model_names = {0: [a.model_name for a in artifacts]}

    model_state, telemetry, blocked = _orchestrate_waves_with_gating(
        wave_to_model_names=wave_to_model_names,
        dbt_project_dir=dbt_project_dir,
        max_fix_attempts=state.max_fix_attempts,
        dlq_dir=dlq_dir,
        checkpoint_dir=checkpoint_dir,
        bypass_wave=bypass_wave,
        stop_on_first_error=stop_on_first_error,
    )
    state.wave_telemetry = telemetry
    failed = [name for name, st in model_state.items() if st in {"HITL_REQUIRED", "FAILED", "REJECTED"}]
    if blocked and failed:
        state.status = MigrationStatus.HITL_REQUIRED
        state.review_required = failed
    elif failed:
        state.status = MigrationStatus.PARTIAL
        state.review_required = failed
    elif model_state:
        state.status = MigrationStatus.SUCCESS
    else:
        state.status = MigrationStatus.FAILED
    return state, checkpoint, written


def apply_ai_fix_from_checkpoint(
    *,
    dbt_project_dir: Path,
    checkpoint_dir: Path,
    model_name: str,
    ai_sql: str,
) -> tuple[int, str]:
    checkpoint_path = checkpoint_dir / f"checkpoint_b_{model_name}.json"
    if not checkpoint_path.is_file():
        return 1, f"checkpoint artifact not found for model: {model_name}"
    tmp_sql_path = checkpoint_dir / f"{model_name}.ai_fix.sql"
    try:
        tmp_sql_path.write_text(ai_sql, encoding="utf-8")
    except OSError as exc:
        return 1, f"failed to write AI fix SQL: {exc}"
    rc, msg = approve_checkpoint_b_sql(
        dbt_project_dir=dbt_project_dir,
        model_name=model_name,
        fixed_sql_path=tmp_sql_path,
        checkpoint_dir=checkpoint_dir,
    )
    try:
        tmp_sql_path.unlink(missing_ok=True)
    except OSError:
        pass
    return rc, msg


def run_wave_stress_test(
    *,
    checkpoint_dir: Path,
    wave_id: str,
    model_names: list[str],
    model_states: dict[str, str],
) -> dict[str, Any]:
    insights = _load_model_insights(checkpoint_dir)
    summary, telemetry = wave_summary_agent(wave_id, model_names, model_states)
    insights.setdefault("waves", {})
    insights["waves"][str(wave_id)] = summary
    insights.setdefault("telemetry", [])
    insights["telemetry"].append({"agent_name": "wave_summary_agent", **telemetry})
    _save_model_insights(checkpoint_dir, insights)
    return summary


def analyze_model_risk_and_scenarios(
    *,
    checkpoint_dir: Path,
    model_name: str,
    sql: str,
) -> dict[str, Any]:
    insights = _load_model_insights(checkpoint_dir)
    risk, t_risk = risk_agent(sql, model_name)
    scenarios, t_sc = scenario_agent(sql, model_name)
    insights.setdefault("models", {}).setdefault(model_name, {})
    insights["models"][model_name]["risk"] = risk
    insights["models"][model_name]["scenarios"] = scenarios.get("scenarios", [])
    insights.setdefault("telemetry", [])
    insights["telemetry"].append({"agent_name": "risk_agent", **t_risk})
    insights["telemetry"].append({"agent_name": "scenario_agent", **t_sc})
    _save_model_insights(checkpoint_dir, insights)
    return insights["models"][model_name]


def generate_synthetic_data_for_model(
    *,
    checkpoint_dir: Path,
    model_name: str,
    schema_columns: list[str],
    approved: bool,
    row_cap: int = 20,
) -> tuple[int, str, str]:
    if not approved:
        return 1, "Synthetic data generation requires explicit approval.", ""
    if row_cap > 50:
        return 1, "Row cap exceeds allowed threshold (max 50).", ""
    payload, telemetry = data_gen_agent(model_name, schema_columns, row_count=row_cap)
    rows = payload.get("complex_mock_data")
    if not isinstance(rows, list):
        rows = []
    target = checkpoint_dir / f"{model_name}_complex_mock_data.json"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    insights = _load_model_insights(checkpoint_dir)
    insights.setdefault("models", {}).setdefault(model_name, {})
    insights["models"][model_name]["synthetic_dataset_path"] = str(target)
    insights["models"][model_name]["data_gen_approved"] = True
    insights["models"][model_name]["row_cap_applied"] = row_cap
    insights.setdefault("telemetry", []).append({"agent_name": "data_gen_agent", **telemetry})
    _save_model_insights(checkpoint_dir, insights)
    return 0, "Synthetic dataset generated.", str(target)


def propose_sql_patch_from_chat(
    *,
    checkpoint_dir: Path,
    model_name: str,
    sql: str,
    question: str,
) -> dict[str, str]:
    payload, telemetry = chat_model_agent(model_name, sql, question)
    insights = _load_model_insights(checkpoint_dir)
    insights.setdefault("models", {}).setdefault(model_name, {})
    insights["models"][model_name]["chat_last"] = payload
    insights.setdefault("telemetry", []).append({"agent_name": "chat_model_agent", **telemetry})
    _save_model_insights(checkpoint_dir, insights)
    return {
        "answer": str(payload.get("answer") or ""),
        "sql_patch_proposal": str(payload.get("sql_patch_proposal") or ""),
    }
