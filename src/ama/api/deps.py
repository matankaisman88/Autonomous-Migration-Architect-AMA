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
    return (get_dbt_project_dir() / "models" / "ama_generated").resolve()


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


def get_hitl_store_dir() -> Path:
    raw = os.environ.get("AMA_HITL_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    live_root = os.environ.get("AMA_LIVE_DATA_DIR", "live_data").strip() or "live_data"
    return (Path(live_root) / ".hitl").expanduser().resolve()


def _parent_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ama_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def hitl_write_path(report_id: str) -> Path:
    """Return a writable path for persisting HITL decisions."""
    report_path = get_report_path(report_id)
    adjacent = report_path.with_suffix(".hitl.json")
    if _parent_writable(report_path.parent):
        return adjacent
    store_dir = get_hitl_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir / f"{report_id}.hitl.json"


def hitl_read_paths(report_id: str) -> list[Path]:
    """Candidate sidecar paths to load (store first, then report-adjacent)."""
    report_path = get_report_path(report_id)
    adjacent = report_path.with_suffix(".hitl.json")
    store = get_hitl_store_dir() / f"{report_id}.hitl.json"
    paths: list[Path] = []
    for candidate in (store, adjacent):
        if candidate not in paths:
            paths.append(candidate)
    return paths


def get_hitl_path(report_id: str) -> Path:
    return hitl_write_path(report_id)

