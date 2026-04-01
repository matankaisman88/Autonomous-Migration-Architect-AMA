"""
Live database Kfar deploy + artifact export under ``live_data/{connection_name}/``.

POST /api/live/start — queue background job (returns ``job_id``).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ama.api.live_jobs import (
    _LIVE_SEM,
    live_job_append_log,
    live_job_create,
    live_job_snapshot,
    live_job_update,
    sanitize_connection_name,
)
from ama.config import project_root
from ama.kfar_supply.deploy import deploy_kfar_live
from ama.kfar_supply.jsonl_gen import build_jsonl_lines
from ama.kfar_supply.spec import KFAR_TABLES
from ama.cli import cmd_run
from ama.mcp.factory import get_schema_provider
from ama.security.credentials import default_data_root, ensure_under_root, redact_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["Live"])

LIVE_REPORT_JSON = "ama_live_report.json"


class LiveStartRequest(BaseModel):
    mode: str = Field(description="sqlserver | oracle | db2")
    connection_name: str = Field(min_length=1, max_length=80)
    connection_string: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    service_name: str | None = Field(default=None, description="Oracle service name (optional)")
    jsonl_lines: int = Field(default=1200, ge=50, le=50_000)
    build_report: bool = Field(
        default=False,
        description="After a successful artifact export, run ama-ingest discovery merge and write JSON report next to artifacts",
    )


def _build_connection_string(body: LiveStartRequest) -> str:
    if body.connection_string and body.connection_string.strip():
        return body.connection_string.strip()
    mode = body.mode.lower().strip()
    if mode in ("sqlserver", "tsql"):
        if not all([body.host, body.port, body.user, body.database]):
            raise ValueError("sqlserver requires host, port, user, database (or connection_string)")
        pwd = "" if body.password is None else body.password
        return (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={body.host},{body.port};"
            f"DATABASE={body.database};"
            f"UID={body.user};"
            f"PWD={pwd};"
            "TrustServerCertificate=yes;"
        )
    if mode == "oracle":
        if not all([body.host, body.port, body.user]):
            raise ValueError("oracle requires host, port, user (or connection_string)")
        pwd = "" if body.password is None else body.password
        svc = (body.service_name or body.database or "XEPDB1").strip()
        return f"{body.user}/{pwd}@{body.host}:{body.port}/{svc}"
    if mode == "db2":
        if not all([body.host, body.port, body.user, body.database]):
            raise ValueError("db2 requires host, port, user, database (or connection_string)")
        pwd_db2 = "" if body.password is None else body.password
        return (
            f"DATABASE={body.database};HOSTNAME={body.host};PORT={body.port};"
            f"PROTOCOL=TCPIP;UID={body.user};PWD={pwd_db2};"
        )
    raise ValueError(f"unsupported mode: {body.mode}")


def _deploy_dialect(mode: str) -> str:
    m = mode.lower().strip()
    if m == "sqlserver":
        return "tsql"
    return m


def _write_artifacts(out_dir: Path, jsonl_lines: int, log: Any) -> None:
    ddl_dir = out_dir / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for t in KFAR_TABLES:
        rel = f"ddl/{t.ddl_json_filename}"
        manifest[t.full_key] = rel
        p = ddl_dir / t.ddl_json_filename
        p.write_text(
            json.dumps({"columns": list(t.columns)}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    (out_dir / "manifest.json").write_text(
        json.dumps({"_comment": "Kfar live export", **manifest}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logs_dir = out_dir / "sql_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    lines = build_jsonl_lines(jsonl_lines)
    lp = logs_dir / "prod.jsonl"
    with lp.open("w", encoding="utf-8") as f:
        for row in lines:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log(f"wrote artifacts under {redact_path(out_dir, keep_segments=3)}")
    log(f"Full artifact path: {out_dir.resolve()}")


def _live_report_arg_namespace(out_dir: Path, report_out: Path) -> argparse.Namespace:
    """Minimal ``argparse.Namespace`` for :func:`ama.cli.cmd_run` (live export folder)."""
    repo = project_root()
    root = out_dir.resolve()
    sql_log = (root / "sql_logs" / "prod.jsonl").resolve()
    manifest = (root / "manifest.json").resolve()
    ddl_fallback = (root / "ddl" / "dbo_orders.json").resolve()
    comms = (repo / "sample_data" / "kfar_supply" / "comms").resolve()
    git_sql = (repo / "sample_data" / "kfar_supply" / "git_sql").resolve()
    gloss_primary = (repo / "sample_data" / "kfar_supply" / "glossary" / "kfar_glossary.json").resolve()
    gloss_dirty = (repo / "sample_data" / "kfar_supply" / "glossary" / "kfar_glossary_dirty.json").resolve()
    return argparse.Namespace(
        benchmark=False,
        stress=False,
        benchmark_results=None,
        data_root=str(root),
        sql_logs=[str(sql_log)],
        comms_dir=str(comms) if comms.is_dir() else None,
        git_root=str(git_sql) if git_sql.is_dir() else None,
        env="prod",
        skip_vectors=True,
        out_file=str(report_out.resolve()),
        out=None,
        ddl_columns=str(ddl_fallback),
        ddl_manifest=str(manifest),
        glossary=str(gloss_primary) if gloss_primary.is_file() else None,
        glossary_dirty=str(gloss_dirty) if gloss_dirty.is_file() else None,
        no_ddl_merge=False,
        format="json",
        merge_floor=None,
        confirmed_threshold=None,
        discovery_mode=True,
        no_target=False,
        discovery_merge_all=True,
        discovery_merge_max=None,
        discovery_merge_n=10,
        migration_context="dbo.orders",
        target_schema=None,
        target_table=None,
    )


def _run_live_report_build(out_dir: Path, log: Callable[[str], None]) -> tuple[str | None, str | None]:
    """
    Run :func:`ama.cli.cmd_run` in-process on exported ``live_data/{name}/`` artifacts.
    Returns ``(absolute_report_path, error_message)``.
    """
    root = out_dir.resolve()
    sql_log = (root / "sql_logs" / "prod.jsonl").resolve()
    manifest = (root / "manifest.json").resolve()
    ddl_fallback = (root / "ddl" / "dbo_orders.json").resolve()
    report_out = (root / LIVE_REPORT_JSON).resolve()
    for label, p in (
        ("sql_logs/prod.jsonl", sql_log),
        ("manifest.json", manifest),
        ("ddl/dbo_orders.json", ddl_fallback),
    ):
        if not p.is_file():
            return None, f"report build skipped — missing {label}"

    ns = _live_report_arg_namespace(out_dir, report_out)
    log("Running AMA report build (cmd_run in-process) …")
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = cmd_run(ns)
    except Exception as exc:
        logger.exception("live report cmd_run failed")
        return None, f"report build crashed: {exc}"

    merged = (buf_out.getvalue() + "\n" + buf_err.getvalue()).strip()
    if merged:
        for line in merged.splitlines()[-20:]:
            log(f"[ingest] {line}")
    if rc != 0:
        return None, f"ingestion exited with code {rc}"

    if not report_out.is_file():
        return None, "report JSON was not written"
    log(f"Report written: {redact_path(report_out, keep_segments=3)}")
    return str(report_out), None


def _run_live_worker(*, job_id: str, body: LiveStartRequest) -> None:
    def log(msg: str) -> None:
        live_job_append_log(job_id, msg)

    errors: list[str] = []
    try:
        _LIVE_SEM.acquire()
        live_job_update(job_id, status="running", stage="validate_connection", percent=5)
        log("Acquired ingestion slot")
        log(f"Server: build_report={body.build_report} (rebuild the api image if this stays false when the UI box is checked)")

        try:
            conn_str = _build_connection_string(body)
        except ValueError as exc:
            live_job_update(
                job_id,
                status="failure",
                stage="failed",
                percent=100,
                errors=[str(exc)],
            )
            return

        mode = body.mode.lower().strip()
        if mode == "tsql":
            mode = "sqlserver"

        provider = None
        try:
            provider = get_schema_provider(mode=mode, connection_string=conn_str, timeout_seconds=30)
            if not provider.ping():
                raise RuntimeError("Connection test failed (ping returned False)")
        except Exception as exc:
            err = str(exc)
            logger.exception("Live connection validate failed")
            live_job_update(
                job_id,
                status="failure",
                stage="validate_connection",
                percent=100,
                errors=[err],
            )
            log(f"Validate failed: {err}")
            return
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:
                    pass

        log("Connection OK")
        dd = _deploy_dialect(body.mode)
        try:
            live_job_update(job_id, stage="deploy_kfar", percent=25)
            deploy_kfar_live(dd, conn_str, log=log, timeout_seconds=120)
            log("Kfar DDL/DML deploy completed")
        except Exception as exc:
            err = str(exc)
            errors.append(f"deploy: {err}")
            logger.exception("Kfar deploy failed")
            live_job_update(
                job_id,
                status="failure",
                stage="deploy_kfar",
                percent=100,
                errors=errors,
            )
            log(f"Deploy failed: {err}")
            return

        root = default_data_root()
        safe_name = sanitize_connection_name(body.connection_name)
        out_dir = (root / "live_data" / safe_name).resolve()
        ensure_under_root(out_dir, root.resolve())

        final_status = "success"
        try:
            live_job_update(job_id, stage="write_artifacts", percent=70)
            if out_dir.exists():
                log("Overwriting prior live_data export for this connection name")
            out_dir.mkdir(parents=True, exist_ok=True)
            _write_artifacts(out_dir, int(body.jsonl_lines), log)
        except Exception as exc:
            err = str(exc)
            errors.append(f"artifacts: {err}")
            logger.exception("Artifact write failed")
            final_status = "partial"
            log(f"Artifact error: {err}")

        report_path: str | None = None
        report_build_error: str | None = None
        man_path = out_dir / "manifest.json"
        if body.build_report:
            if not man_path.is_file():
                msg = "report skipped — manifest.json missing after export"
                log(msg)
                errors.append(msg)
                if final_status == "success":
                    final_status = "partial"
            else:
                live_job_update(job_id, stage="ama_report", percent=88)
                report_path, report_build_error = _run_live_report_build(out_dir, log)
                if report_build_error:
                    errors.append(report_build_error)
                    if final_status == "success":
                        final_status = "partial"
        elif man_path.is_file():
            log("build_report=false — skipping AMA JSON report (enable on the Live page or pass build_report: true)")

        done_payload: dict[str, Any] = {
            "status": final_status,
            "stage": "done",
            "percent": 100,
            "errors": errors,
        }
        if body.build_report:
            done_payload["report_path"] = report_path
            done_payload["report_build_error"] = report_build_error
        live_job_update(job_id, **done_payload)
        log(f"Finished with status {final_status}")
    finally:
        try:
            _LIVE_SEM.release()
        except Exception:
            pass


@router.post("/start")
def start_live_ingestion(body: LiveStartRequest) -> dict[str, Any]:
    try:
        safe = sanitize_connection_name(body.connection_name)
        _ = _build_connection_string(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = live_job_create(
        {
            "connection_name": safe,
            "mode": body.mode,
            "build_report": body.build_report,
        }
    )

    def _run() -> None:
        _run_live_worker(job_id=job_id, body=body)

    threading.Thread(target=_run, name=f"ama-live-{job_id}", daemon=True).start()
    return {"job_id": job_id, "connection_name": safe, "build_report": body.build_report}


@router.get("/job/{job_id}")
def get_live_job(job_id: str) -> dict[str, Any]:
    snap = live_job_snapshot(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap
