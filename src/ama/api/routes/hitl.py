from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ama.api import deps
from ama.business_logic import review_row_signature
from ama.hitl_apply import apply_hitl_to_report

logger = logging.getLogger(__name__)
router = APIRouter()


class HitlDecisionRequest(BaseModel):
    row: dict[str, Any]
    action: str  # approved | rejected | clear


@router.get("/{report_id}")
def hitl_get(report_id: str) -> dict[str, Any]:
    """Load report sidecar HITL decisions from `<report>.hitl.json`."""
    _ = deps.get_report(report_id)
    hitl_path = deps.get_hitl_path(report_id)
    if not hitl_path.is_file():
        return {"version": 1, "decisions": {}}
    try:
        payload = json.loads(hitl_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"version": 1, "decisions": {}}
        return payload
    except Exception as exc:
        logger.exception("hitl load failed")
        raise HTTPException(status_code=500, detail=f"HITL load failed: {exc}") from exc


@router.post("/{report_id}/decision")
def hitl_set_decision(report_id: str, body: HitlDecisionRequest) -> dict[str, Any]:
    """Persist one HITL decision by stable review-row signature."""
    _ = deps.get_report(report_id)
    action = str(body.action or "").strip().lower()
    if action not in {"approved", "rejected", "clear"}:
        raise HTTPException(status_code=400, detail="action must be approved|rejected|clear")
    hitl_path = deps.get_hitl_path(report_id)
    try:
        current = {"version": 1, "decisions": {}}
        if hitl_path.is_file():
            loaded = json.loads(hitl_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        decisions = current.get("decisions")
        if not isinstance(decisions, dict):
            decisions = {}
        sig = review_row_signature(body.row)
        if action == "clear":
            decisions.pop(sig, None)
        else:
            decisions[sig] = {"action": action}
        current["decisions"] = decisions
        hitl_path.parent.mkdir(parents=True, exist_ok=True)
        hitl_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"signature": sig, "action": action, "saved": True}
    except Exception as exc:
        logger.exception("hitl save failed")
        raise HTTPException(status_code=500, detail=f"HITL save failed: {exc}") from exc


@router.post("/{report_id}/apply")
def hitl_apply(report_id: str) -> dict[str, Any]:
    """Apply HITL sidecar decisions to loaded report using apply_hitl_to_report."""
    report = deps.get_report(report_id)
    hitl_path = deps.get_hitl_path(report_id)
    try:
        hitl = {"version": 1, "decisions": {}}
        if hitl_path.is_file():
            loaded = json.loads(hitl_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                hitl = loaded
        applied = apply_hitl_to_report(report, hitl)
        deps.REPORT_STORE[report_id] = applied
        am = applied.get("alias_merge") or {}
        return {
            "applied": True,
            "counts": {
                "merged_entities": len(am.get("merged_entities") or []),
                "review_candidates": len(am.get("review_candidates") or []),
                "trash_candidates": len(am.get("trash_candidates") or []),
            },
        }
    except Exception as exc:
        logger.exception("hitl apply failed")
        raise HTTPException(status_code=500, detail=f"HITL apply failed: {exc}") from exc

