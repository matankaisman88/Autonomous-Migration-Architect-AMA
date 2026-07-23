from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ama.ai_query_helper import OpenAIAuthError, OpenAIQueryError, OpenAIRateLimitError, query_openai_json
from ama.dbt_migration.agent_prompts import get_agent_prompt
from ama.dbt_migration.paths import find_model_sql_path, model_sql_path_for_write
from ama.dbt_migration.models import (
    CheckpointBArtifact,
    CheckpointBHistoryItem,
    DlqRecord,
    ExecutionResult,
    MigrationSessionState,
    MigrationStatus,
    ModelExecutionTrace,
    ModelRunState,
    RunAttempt,
    RunnerFinalStatus,
)
from ama.env_resolver import has_openai_api_key

logger = logging.getLogger(__name__)


def _is_dbt_invocation(command: list[str]) -> bool:
    if not command:
        return False
    if command[0] == "dbt":
        return True
    return len(command) >= 3 and command[1] == "-m" and command[2] == "dbt"


def _resolve_dbt_command(command: list[str]) -> list[str]:
    if not command or command[0] != "dbt":
        return command
    if shutil.which("dbt"):
        return command
    # Fallback for environments where console scripts are missing but dbt is installed
    # in the active interpreter.
    return [sys.executable, "-m", "dbt", *command[1:]]


def _run_command(command: list[str], cwd: Path) -> tuple[int, str, str]:
    command = _resolve_dbt_command(list(command))
    # dbt defaults `--profiles-dir` to `~/.dbt`. On fresh machines the folder might not
    # exist, which causes dbt to fail before it even reads `profiles.yml`.
    # We create the default profiles directory as a best-effort guard.
    if _is_dbt_invocation(command):
        profiles_dir: Path | None = None
        if "--profiles-dir" in command:
            try:
                idx = command.index("--profiles-dir")
                if idx + 1 < len(command):
                    profiles_dir = Path(str(command[idx + 1])).expanduser()
            except ValueError:
                profiles_dir = None
        if profiles_dir is None:
            env_profiles = os.environ.get("DBT_PROFILES_DIR")
            profiles_dir = Path(env_profiles).expanduser() if env_profiles else (Path.home() / ".dbt")
        try:
            profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # If mkdir fails, we still attempt to run; dbt will report the real error.
            pass

        # Ensure dbt reads from the same directory we just prepared.
        if "--profiles-dir" not in command:
            command.extend(["--profiles-dir", str(profiles_dir)])

        # Auto-pick a usable profile/target from profiles.yml.
        # This avoids common UX failures like "Could not find profile named 'default'".
        profiles_yml = profiles_dir / "profiles.yml"
        def _write_duckdb_profiles_template() -> None:
            """
            Create a minimal DuckDB `profiles.yml`.

            Important: use *single quotes* for Windows paths so YAML doesn't treat
            backslash sequences like `\t` as escapes.
            """
            duckdb_db_path = (cwd / "target" / "duckdb.db").resolve()
            profiles_yml.write_text(
                "\n".join(
                    [
                        "default:",
                        "  target: dev",
                        "  outputs:",
                        "    dev:",
                        "      type: duckdb",
                        f"      path: '{str(duckdb_db_path)}'",
                        "      threads: 1",
                        "      schema: main",
                    ]
                ),
                encoding="utf-8",
            )

        needs_template = not profiles_yml.is_file()
        if not needs_template:
            # If the user has a malformed profiles.yml (often from our previous
            # auto-generation with double-quoted Windows paths), rewrite it.
            try:
                import yaml  # type: ignore

                yaml.safe_load(profiles_yml.read_text(encoding="utf-8"))
            except Exception:
                needs_template = True

        if needs_template:
            try:
                profiles_dir.mkdir(parents=True, exist_ok=True)
                _write_duckdb_profiles_template()
            except OSError:
                # Best-effort only; dbt will report the real error if it can't read profiles.
                pass
        if profiles_yml.is_file() and ("--profile" not in command or "--target" not in command):
            try:
                import yaml  # type: ignore

                payload = yaml.safe_load(profiles_yml.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload:
                    env_profile = os.environ.get("DBT_PROFILE_NAME")
                    profile_name: str | None = None
                    if env_profile:
                        profile_name = env_profile
                    elif "default" in payload:
                        profile_name = "default"
                    else:
                        # first top-level profile key
                        profile_name = str(next(iter(payload.keys())))

                    profile_block = payload.get(profile_name) if profile_name else None
                    target_name: str | None = None
                    if isinstance(profile_block, dict):
                        # Prefer the configured profile target; otherwise first outputs key.
                        configured_target = profile_block.get("target")
                        if isinstance(configured_target, str) and configured_target:
                            target_name = configured_target
                        outputs = profile_block.get("outputs")
                        if (not target_name) and isinstance(outputs, dict) and outputs:
                            # outputs keys are the target names
                            target_name = str(next(iter(outputs.keys())))

                    if profile_name and "--profile" not in command:
                        command.extend(["--profile", profile_name])
                    if target_name and "--target" not in command:
                        command.extend(["--target", target_name])
            except Exception:
                # Best-effort only; if parsing fails, dbt will raise the true error.
                pass
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except OSError as exc:
        return 1, "", f"OS error while executing {' '.join(command)}: {exc}"


def _extract_known_error(stderr: str, stdout: str) -> str:
    hay = f"{stderr}\n{stdout}".lower()
    if "column does not exist" in hay:
        return "column does not exist"
    if "type mismatch" in hay:
        return "type mismatch"
    if "syntax error" in hay:
        return "syntax error"
    return "unknown dbt error"


def _looks_like_select_sql(sql: str) -> bool:
    text = (sql or "").strip().lower()
    return "select" in text and "from" in text


def _apply_corrected_sql(dbt_project_dir: Path, model_name: str, corrected_sql: str) -> bool:
    if not _looks_like_select_sql(corrected_sql):
        return False
    sql_path = model_sql_path_for_write(dbt_project_dir=dbt_project_dir, model_name=model_name)
    try:
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path.write_text(corrected_sql, encoding="utf-8")
    except OSError:
        return False
    return True


def _run_fix_agent(
    *,
    model_name: str,
    error_log: str,
    failed_sql: str,
    attempt_history: list[dict[str, Any]],
) -> tuple[str, str, int, float]:
    user_prompt = json.dumps(
        {
            "model_name": model_name,
            "error_log": error_log,
            "failed_sql": failed_sql,
            "attempt_history": attempt_history,
            "response_schema": {
                "corrected_sql": "string",
                "error_analysis": "string",
                "confidence": "float_0_to_1",
            },
        },
        ensure_ascii=False,
    )
    result = query_openai_json(
        system_prompt=get_agent_prompt("fix_agent"),
        user_prompt=user_prompt,
        max_tokens=2200,
        timeout_seconds=45,
        temperature=0.0,
    )
    payload = result.payload
    corrected_sql = str(payload.get("corrected_sql") or "")
    error_analysis = str(payload.get("error_analysis") or "")
    confidence = float(payload.get("confidence") or 0.0)
    return corrected_sql, error_analysis, result.tokens_used, confidence


def _persist_dlq_record(dlq_dir: Path, record: DlqRecord) -> None:
    dlq_dir.mkdir(parents=True, exist_ok=True)
    dlq_path = dlq_dir / "dlq_records.jsonl"
    with dlq_path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json(ensure_ascii=False) + "\n")


def _checkpoint_b_path(checkpoint_dir: Path, model_name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in model_name)
    return checkpoint_dir / f"checkpoint_b_{safe}.json"


def _save_checkpoint_b_artifact(
    checkpoint_dir: Path,
    artifact: CheckpointBArtifact,
) -> Path:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    target = _checkpoint_b_path(checkpoint_dir, artifact.model_name)
    tmp = target.with_suffix(".json.tmp")
    # Atomic replace to reduce partial writes / lock race windows.
    tmp.write_text(artifact.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(target))
    return target


def _load_checkpoint_b_artifact(checkpoint_dir: Path, model_name: str) -> CheckpointBArtifact:
    path = _checkpoint_b_path(checkpoint_dir, model_name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CheckpointBArtifact.model_validate(payload)


def _build_dlq_from_checkpoint(
    artifact: CheckpointBArtifact,
    *,
    run_id: str,
    error_stage: str,
) -> DlqRecord:
    return DlqRecord(
        original_payload={
            "model_name": artifact.model_name,
            "sql": artifact.current_sql,
            "attempt_history": artifact.attempt_history,
        },
        error_reason=artifact.error_log,
        error_stage=error_stage,
        timestamp=datetime.now(timezone.utc).isoformat(),
        run_id=run_id,
    )


def _read_records_impacted_from_artifact(dbt_project_dir: Path) -> dict[str, int | None]:
    target_file = dbt_project_dir / "target" / "run_results.json"
    if not target_file.is_file():
        return {}
    try:
        payload = json.loads(target_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int | None] = {}
    results = payload.get("results")
    if not isinstance(results, list):
        return out
    for row in results:
        if not isinstance(row, dict):
            continue
        unique_id = str(row.get("unique_id") or "")
        adapter_response = row.get("adapter_response")
        records = None
        if isinstance(adapter_response, dict):
            for key in ("rows_affected", "rows", "records_affected"):
                if key in adapter_response:
                    raw = adapter_response.get(key)
                    try:
                        records = int(raw) if raw is not None else None
                    except (TypeError, ValueError):
                        records = None
                    break
        if unique_id:
            out[unique_id] = records
    return out


def execute_models_with_fix_loop(
    *,
    dbt_project_dir: Path,
    model_names: list[str],
    max_attempts: int,
    dlq_dir: Path,
    checkpoint_dir: Path,
    patch_callback: Callable[[str, str, int], None] | None = None,
    bootstrap_model_fn: Callable[[str], None] | None = None,
) -> ExecutionResult:
    started = time.perf_counter()
    run_id = uuid4()
    result = ExecutionResult(run_id=run_id, status=RunnerFinalStatus.FAILURE)
    model_results: list[ModelExecutionTrace] = []
    success_count = 0

    for model_name in model_names:
        trace = ModelExecutionTrace(model_name=model_name)
        original_sql = ""
        suggested_sql = ""
        fix_error_analysis = ""
        fix_confidence = 0.0
        fix_tokens_used = 0
        fix_fallback = False
        auth_error = False
        rate_limit_error = False
        sql_path = find_model_sql_path(dbt_project_dir=dbt_project_dir, model_name=model_name)
        if sql_path is not None and sql_path.is_file():
            try:
                original_sql = sql_path.read_text(encoding="utf-8")
            except OSError:
                original_sql = ""
        initial_failed_sql = original_sql
        for attempt_no in range(1, max_attempts + 1):
            command = ["dbt", "run", "--select", model_name]
            rc, out, err = _run_command(command, dbt_project_dir)
            trace.attempts.append(
                RunAttempt(
                    attempt=attempt_no,
                    command=" ".join(command),
                    return_code=rc,
                    stdout=out,
                    stderr=err,
                )
            )
            if rc == 0:
                rc_t, out_t, err_t = _run_command(["dbt", "test", "--select", model_name], dbt_project_dir)
                trace.attempts.append(
                    RunAttempt(
                        attempt=attempt_no,
                        command=f"dbt test --select {model_name}",
                        return_code=rc_t,
                        stdout=out_t,
                        stderr=err_t,
                    )
                )
                if rc_t == 0:
                    trace.state = ModelRunState.SUCCESS
                    trace.fix_loop_count = attempt_no - 1
                    success_count += 1
                    break
                err = err_t or out_t

            trace.last_error_log = (err or out).strip()
            if attempt_no < max_attempts:
                if patch_callback is not None:
                    patch_callback(
                        model_name,
                        _extract_known_error(err or "", out or ""),
                        attempt_no,
                    )
                elif has_openai_api_key():
                    attempt_history = [
                        {
                            "attempt": a.attempt,
                            "command": a.command,
                            "return_code": a.return_code,
                            "stderr": a.stderr[:300],
                            "stdout": a.stdout[:300],
                        }
                        for a in trace.attempts
                    ]
                    try:
                        corrected_sql, error_analysis, tokens_used, confidence = _run_fix_agent(
                            model_name=model_name,
                            error_log=trace.last_error_log,
                            failed_sql=original_sql,
                            attempt_history=attempt_history,
                        )
                        suggested_sql = corrected_sql
                        fix_error_analysis = error_analysis
                        fix_confidence = confidence
                        fix_tokens_used = tokens_used
                        logger.info(
                            "llm_agent_telemetry",
                            extra={
                                "agent_name": "fix_agent",
                                "tokens_used": tokens_used,
                                "confidence": round(confidence, 4),
                                "is_fallback_active": False,
                            },
                        )
                        if corrected_sql and _apply_corrected_sql(dbt_project_dir, model_name, corrected_sql):
                            original_sql = corrected_sql
                            if bootstrap_model_fn is not None:
                                try:
                                    bootstrap_model_fn(model_name)
                                except Exception:
                                    pass
                        else:
                            fix_fallback = True
                            logger.warning(
                                "llm_agent_fallback",
                                extra={
                                    "agent_name": "fix_agent",
                                    "tokens_used": tokens_used,
                                    "confidence": round(confidence, 4),
                                    "is_fallback_active": True,
                                },
                            )
                    except OpenAIAuthError:
                        auth_error = True
                        raise
                    except OpenAIRateLimitError:
                        rate_limit_error = True
                        fix_fallback = True
                        logger.warning(
                            "llm_agent_fallback",
                            extra={
                                "agent_name": "fix_agent",
                                "tokens_used": 0,
                                "confidence": 0.0,
                                "is_fallback_active": True,
                            },
                        )
                    except (OpenAIQueryError, ValueError, TypeError):
                        fix_fallback = True
                        logger.warning(
                            "llm_agent_fallback",
                            extra={
                                "agent_name": "fix_agent",
                                "tokens_used": 0,
                                "confidence": 0.0,
                                "is_fallback_active": True,
                            },
                        )
                logger.info(
                    "model_retry_scheduled",
                    extra={"model_name": model_name, "attempt_number": attempt_no},
                )
                continue

            trace.state = ModelRunState.REJECTED
            trace.fix_loop_count = max_attempts
            history = [
                CheckpointBHistoryItem(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    error_snippet=(a.stderr or a.stdout)[:300],
                    action_taken="retry" if i < len(trace.attempts) - 1 else "checkpoint_b_generated",
                ).model_dump()
                for i, a in enumerate(trace.attempts)
            ]
            artifact = CheckpointBArtifact(
                model_name=model_name,
                current_sql=original_sql,
                failed_sql=initial_failed_sql,
                error_log=trace.last_error_log,
                attempt_history=history,
                suggested_sql=suggested_sql,
                fix_agent_error_analysis=fix_error_analysis,
                fix_confidence=fix_confidence,
                tokens_used=fix_tokens_used,
                is_fallback_active=fix_fallback,
                auth_error=auth_error,
                rate_limit_error=rate_limit_error,
            )
            _save_checkpoint_b_artifact(checkpoint_dir, artifact)
            trace.state = ModelRunState.HITL_REQUIRED
            logger.warning(
                "Model %s requires manual review. Checkpoint B artifact generated.",
                model_name,
            )
        model_results.append(trace)

    result.model_results = model_results
    result.fix_loop_count = {m.model_name: m.fix_loop_count for m in model_results}
    records = _read_records_impacted_from_artifact(dbt_project_dir)
    result.records_impacted = records
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result.duration_ms = elapsed_ms
    hitl_count = sum(1 for m in model_results if m.state == ModelRunState.HITL_REQUIRED)
    if success_count == len(model_names) and model_names:
        result.status = RunnerFinalStatus.SUCCESS
    elif success_count > 0:
        result.status = RunnerFinalStatus.PARTIAL
    else:
        result.status = RunnerFinalStatus.FAILURE
    if hitl_count > 0:
        result.summary_status = "REVIEW_REQUIRED"
    elif result.status == RunnerFinalStatus.SUCCESS:
        result.summary_status = "SUCCESS"
    else:
        result.summary_status = "FAILED"
    if model_results:
        errs = [m.last_error_log for m in model_results if m.last_error_log]
        if errs:
            result.last_error = errs[-1]
    logger.info(
        "dbt_execution_complete",
        extra={
            "run_id": str(result.run_id),
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "model_count": len(model_names),
        },
    )
    return result


def approve_checkpoint_b_sql(
    *,
    dbt_project_dir: Path,
    model_name: str,
    fixed_sql_path: Path,
    checkpoint_dir: Path,
) -> tuple[int, str]:
    if not fixed_sql_path.is_file():
        return 1, f"fixed SQL not found: {fixed_sql_path}"
    sql_target = model_sql_path_for_write(dbt_project_dir=dbt_project_dir, model_name=model_name)
    try:
        content = fixed_sql_path.read_text(encoding="utf-8")
        sql_target.parent.mkdir(parents=True, exist_ok=True)
        tmp = sql_target.with_suffix(".sql.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(sql_target))
    except OSError as exc:
        return 1, f"failed to write model SQL: {exc}"
    rc, out, err = _run_command(["dbt", "run", "--select", model_name], dbt_project_dir)
    if rc == 0:
        cp_path = _checkpoint_b_path(checkpoint_dir, model_name)
        if cp_path.is_file():
            try:
                cp_path.unlink()
            except OSError:
                pass
        return 0, out
    return rc, err or out


def reject_checkpoint_b_to_dlq(
    *,
    model_name: str,
    checkpoint_dir: Path,
    dlq_dir: Path,
) -> tuple[int, str]:
    cp_path = _checkpoint_b_path(checkpoint_dir, model_name)
    if not cp_path.is_file():
        return 1, f"checkpoint artifact not found for model: {model_name}"
    try:
        artifact = _load_checkpoint_b_artifact(checkpoint_dir, model_name)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return 1, f"invalid checkpoint artifact for {model_name}: {exc}"
    dlq_record = _build_dlq_from_checkpoint(
        artifact,
        run_id=str(uuid4()),
        error_stage="CHECKPOINT_B_REJECTION",
    )
    _persist_dlq_record(dlq_dir, dlq_record)
    try:
        cp_path.unlink()
    except OSError:
        pass
    return 0, f"Model {model_name} moved to DLQ"


def run_dbt_with_fix_loop(
    dbt_project_dir: Path,
    state: MigrationSessionState,
) -> MigrationSessionState:
    if not state.checkpoint_approved:
        state.status = MigrationStatus.REVIEW_REQUIRED
        return state

    state.status = MigrationStatus.RUNNING
    for attempt_no in range(1, state.max_fix_attempts + 1):
        code_run, out_run, err_run = _run_command(["dbt", "run"], dbt_project_dir)
        state.attempts.append(
            RunAttempt(
                attempt=attempt_no,
                command="dbt run",
                return_code=code_run,
                stdout=out_run,
                stderr=err_run,
            )
        )
        if code_run != 0:
            state.status = (
                MigrationStatus.FIXING
                if attempt_no < state.max_fix_attempts
                else MigrationStatus.REVIEW_REQUIRED
            )
            continue

        code_test, out_test, err_test = _run_command(["dbt", "test"], dbt_project_dir)
        state.attempts.append(
            RunAttempt(
                attempt=attempt_no,
                command="dbt test",
                return_code=code_test,
                stdout=out_test,
                stderr=err_test,
            )
        )
        if code_test == 0:
            state.status = MigrationStatus.SUCCESS
            return state
        state.status = (
            MigrationStatus.FIXING
            if attempt_no < state.max_fix_attempts
            else MigrationStatus.HITL_REQUIRED
        )

    state.status = MigrationStatus.HITL_REQUIRED if state.attempts else MigrationStatus.FAILED
    return state
