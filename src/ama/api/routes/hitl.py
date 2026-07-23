from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ama.api import deps
from ama.business_logic import review_row_signature
from ama.hitl_apply import apply_hitl_to_report

logger = logging.getLogger(__name__)
router = APIRouter()


class HitlDecisionRequest(BaseModel):
    row: dict[str, Any]
    action: str  # approved | rejected | clear
    auto_apply: bool = True


class HitlBatchDecisionRequest(BaseModel):
    action: str  # approved | rejected
    min_confidence: float | None = None
    max_confidence: float | None = None
    signatures: list[str] | None = None
    source_table: str | None = None
    auto_apply: bool = True


def _load_hitl_sidecar(hitl_path: Path) -> dict[str, Any]:
    if not hitl_path.is_file():
        return {"version": 1, "decisions": {}}
    try:
        payload = json.loads(hitl_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "decisions": {}}
    return payload if isinstance(payload, dict) else {"version": 1, "decisions": {}}


def _load_hitl_for_report(report_id: str) -> dict[str, Any]:
    for path in deps.hitl_read_paths(report_id):
        loaded = _load_hitl_sidecar(path)
        if loaded.get("decisions"):
            return loaded
        if path.is_file():
            return loaded
    return {"version": 1, "decisions": {}}


def _save_hitl_sidecar(hitl_path: Path, data: dict[str, Any]) -> None:
    hitl_path.parent.mkdir(parents=True, exist_ok=True)
    hitl_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _counts_from_report(report: dict[str, Any]) -> dict[str, int]:
    am = report.get("alias_merge") or {}
    return {
        "merged_entities": len(am.get("merged_entities") or []),
        "review_candidates": len(am.get("review_candidates") or []),
        "trash_candidates": len(am.get("trash_candidates") or []),
    }


def _merge_confidence(row: dict[str, Any]) -> float:
    try:
        return float(row.get("merge_confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _decision_action(decisions: dict[str, Any], sig: str) -> str | None:
    d = decisions.get(sig)
    if not isinstance(d, dict):
        return None
    action = str(d.get("action") or "").strip().lower()
    return action if action in {"approved", "rejected"} else None


def _build_queue_items(
    report: dict[str, Any],
    hitl: dict[str, Any],
    *,
    source_table: str | None = None,
) -> list[dict[str, Any]]:
    am = report.get("alias_merge") or {}
    reviews = [r for r in (am.get("review_candidates") or []) if isinstance(r, dict)]
    if source_table:
        reviews = [r for r in reviews if str(r.get("source_table") or "") == source_table]
    decisions_raw = hitl.get("decisions") if isinstance(hitl, dict) else None
    decisions: dict[str, Any] = decisions_raw if isinstance(decisions_raw, dict) else {}
    items: list[dict[str, Any]] = []
    for row in reviews:
        sig = review_row_signature(row)
        action = _decision_action(decisions, sig)
        items.append(
            {
                "signature": sig,
                "row": row,
                "decision": action,
                "status": "pending" if action is None else action,
                "merge_confidence": _merge_confidence(row),
            }
        )
    return items


def _build_rejected_items(report: dict[str, Any], *, source_table: str | None = None) -> list[dict[str, Any]]:
    am = report.get("alias_merge") or {}
    items: list[dict[str, Any]] = []
    for row in am.get("trash_candidates") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("category") or "").strip() != "hitl_rejected":
            continue
        st = str(row.get("source_table") or "")
        if source_table and st != source_table:
            continue
        items.append(
            {
                "signature": review_row_signature(row),
                "row": row,
                "decision": "rejected",
                "status": "rejected",
                "merge_confidence": _merge_confidence(row),
            }
        )
    return items


def _apply_to_report_store(report_id: str, report: dict[str, Any], hitl: dict[str, Any]) -> dict[str, Any]:
    applied = apply_hitl_to_report(report, hitl)
    deps.REPORT_STORE[report_id] = applied
    return applied


def _persist_decision(
    report_id: str,
    row: dict[str, Any],
    action: str,
    *,
    auto_apply: bool,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in {"approved", "rejected", "clear"}:
        raise HTTPException(status_code=400, detail="action must be approved|rejected|clear")
    hitl_path = deps.get_hitl_path(report_id)
    current = _load_hitl_for_report(report_id)
    decisions = current.get("decisions")
    if not isinstance(decisions, dict):
        decisions = {}
    sig = review_row_signature(row)
    if action == "clear":
        decisions.pop(sig, None)
    else:
        decisions[sig] = {
            "action": action,
            "row": {
                "source_table": row.get("source_table"),
                "legacy_name": row.get("legacy_name"),
                "suggested_ddl": row.get("suggested_ddl"),
            },
        }
    current["decisions"] = decisions
    _save_hitl_sidecar(hitl_path, current)
    result: dict[str, Any] = {"signature": sig, "action": action, "saved": True}
    if auto_apply:
        report = deps.get_report(report_id)
        applied = _apply_to_report_store(report_id, report, current)
        result["applied"] = True
        result["counts"] = _counts_from_report(applied)
        result["pending_count"] = result["counts"]["review_candidates"]
    return result


@router.get("/{report_id}/queue")
def hitl_queue(
    report_id: str,
    source_table: str | None = Query(default=None),
) -> dict[str, Any]:
    """Review inbox: pending alias mappings plus saved decisions."""
    report = deps.get_report(report_id)
    hitl = _load_hitl_for_report(report_id)
    items = _build_queue_items(report, hitl, source_table=source_table)
    rejected_items = _build_rejected_items(report, source_table=source_table)
    pending = sum(1 for i in items if i["status"] == "pending")
    approved = sum(1 for i in items if i["status"] == "approved")
    rejected = len(rejected_items)
    return {
        "items": items,
        "rejected_items": rejected_items,
        "pending_count": pending,
        "approved_count": approved,
        "rejected_count": rejected,
        "counts": _counts_from_report(report),
    }


@router.get("/{report_id}")
def hitl_get(report_id: str) -> dict[str, Any]:
    """Load HITL decisions for a loaded report."""
    _ = deps.get_report(report_id)
    try:
        return _load_hitl_for_report(report_id)
    except Exception as exc:
        logger.exception("hitl load failed")
        raise HTTPException(status_code=500, detail=f"HITL load failed: {exc}") from exc


@router.post("/{report_id}/decision")
def hitl_set_decision(report_id: str, body: HitlDecisionRequest) -> dict[str, Any]:
    """Persist one mapping decision and optionally apply it to the loaded report."""
    _ = deps.get_report(report_id)
    try:
        return _persist_decision(report_id, body.row, body.action, auto_apply=body.auto_apply)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("hitl save failed")
        raise HTTPException(status_code=500, detail=f"HITL save failed: {exc}") from exc


@router.post("/{report_id}/decisions/batch")
def hitl_batch_decision(report_id: str, body: HitlBatchDecisionRequest) -> dict[str, Any]:
    """Apply the same decision to many review rows (confidence filters or explicit signatures)."""
    report = deps.get_report(report_id)
    action = str(body.action or "").strip().lower()
    if action not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="action must be approved|rejected")
    hitl_path = deps.get_hitl_path(report_id)
    current = _load_hitl_for_report(report_id)
    decisions = current.get("decisions")
    if not isinstance(decisions, dict):
        decisions = {}
    sig_filter = set(body.signatures) if body.signatures is not None else None
    matched = 0
    for row in _build_queue_items(report, current, source_table=body.source_table):
        if row["status"] != "pending":
            continue
        sig = str(row["signature"])
        if sig_filter is not None and sig not in sig_filter:
            continue
        conf = float(row.get("merge_confidence") or 0.0)
        if body.min_confidence is not None and conf < body.min_confidence:
            continue
        if body.max_confidence is not None and conf > body.max_confidence:
            continue
        raw = row.get("row")
        if not isinstance(raw, dict):
            continue
        decisions[sig] = {
            "action": action,
            "row": {
                "source_table": raw.get("source_table"),
                "legacy_name": raw.get("legacy_name"),
                "suggested_ddl": raw.get("suggested_ddl"),
            },
        }
        matched += 1
    current["decisions"] = decisions
    _save_hitl_sidecar(hitl_path, current)
    result: dict[str, Any] = {"matched": matched, "action": action, "saved": True}
    if body.auto_apply:
        report = deps.get_report(report_id)
        applied = _apply_to_report_store(report_id, report, current)
        result["applied"] = True
        result["counts"] = _counts_from_report(applied)
        result["pending_count"] = result["counts"]["review_candidates"]
    return result


@router.post("/{report_id}/apply")
def hitl_apply(report_id: str) -> dict[str, Any]:
    """Apply all HITL sidecar decisions to the loaded report."""
    report = deps.get_report(report_id)
    try:
        hitl = _load_hitl_for_report(report_id)
        applied = _apply_to_report_store(report_id, report, hitl)
        counts = _counts_from_report(applied)
        return {
            "applied": True,
            "counts": counts,
            "pending_count": counts["review_candidates"],
        }
    except Exception as exc:
        logger.exception("hitl apply failed")
        raise HTTPException(status_code=500, detail=f"HITL apply failed: {exc}") from exc
