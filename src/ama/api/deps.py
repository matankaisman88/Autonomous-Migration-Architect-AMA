from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

REPORT_STORE: dict[str, dict[str, Any]] = {}
PATH_STORE: dict[str, Path] = {}


def make_report_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]


def get_report(report_id: str) -> dict[str, Any]:
    report = REPORT_STORE.get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not loaded.")
    return report


def get_report_path(report_id: str) -> Path:
    path = PATH_STORE.get(report_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Report path not found.")
    return path


def get_dbt_project_dir() -> Path:
    return Path(os.environ.get("AMA_DBT_PROJECT_DIR", "dbt_project")).expanduser().resolve()


def get_output_dir(report_id: str) -> Path:
    _ = report_id
    raw = os.environ.get("AMA_OUTPUT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (get_dbt_project_dir() / "models").resolve()


def get_checkpoint_dir() -> Path:
    raw = os.environ.get("AMA_CHECKPOINT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (get_dbt_project_dir() / "target" / "checkpoints").resolve()


def get_dlq_dir() -> Path:
    raw = os.environ.get("AMA_DLQ_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (get_checkpoint_dir() / "dlq").resolve()


def get_hitl_path(report_id: str) -> Path:
    report_path = get_report_path(report_id)
    return report_path.with_suffix(".hitl.json")

