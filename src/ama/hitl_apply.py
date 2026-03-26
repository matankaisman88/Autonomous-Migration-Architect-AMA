"""
Apply HITL sidecar decisions (approve/reject) to an ingestion report JSON.

The dashboard writes `<report>.hitl.json` next to the report; this module merges
those decisions into `alias_merge` so Excel / downstream consumers see confirmed
rows in `merged_entities` instead of `review_candidates`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ama.business_logic import review_row_signature

_DEFAULT_STATS = {"select": 0, "where": 0, "join_on": 0, "group_by": 0, "order_by": 0}


def decision_from_queue(queue: str) -> str:
    q = str(queue or "").lower().strip()
    if q == "green":
        return "bulk_approved"
    if q == "yellow":
        return "review_required"
    return "blocked"


def _merged_entity_from_approved(row: dict[str, Any]) -> dict[str, Any]:
    leg = str(row.get("legacy_name") or "")
    ddl = str(row.get("suggested_ddl") or "")
    st = str(row.get("source_table") or "")
    try:
        conf = float(row.get("merge_confidence", 0.8))
    except (TypeError, ValueError):
        conf = 0.8
    conf = min(0.98, max(conf, 0.85))
    strat = str(row.get("strategy") or "review")
    cite = str(row.get("citation") or "").strip()
    citations = [f"HITL approved{f': {cite}' if cite else ''}"]
    return {
        "canonical_column": ddl,
        "source_columns": [leg] if leg else [],
        "merge_confidence": conf,
        "strategies": [strat, "hitl_approved"],
        "citations": citations,
        "source_table": st,
    }


def _trash_from_rejected(row: dict[str, Any]) -> dict[str, Any]:
    stats = row.get("stats")
    if not isinstance(stats, dict):
        stats = dict(_DEFAULT_STATS)
    try:
        conf = float(row.get("merge_confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "legacy_name": str(row.get("legacy_name") or ""),
        "suggested_ddl": str(row.get("suggested_ddl") or ""),
        "merge_confidence": conf,
        "category": "hitl_rejected",
        "citation": "Marked rejected in HITL review.",
        "strategy": str(row.get("strategy") or ""),
        "stats": stats,
        "source_table": str(row.get("source_table") or ""),
    }


def apply_hitl_to_report(report: dict[str, Any], hitl: dict[str, Any]) -> dict[str, Any]:
    """
    Return a deep copy of `report` with HITL decisions applied to `alias_merge`.

    - **approved**: row removed from `review_candidates`, appended to `merged_entities`.
    - **rejected**: row removed from `review_candidates`, appended to `trash_candidates`.
    """
    out = copy.deepcopy(report)
    am = out.get("alias_merge")
    if not isinstance(am, dict):
        am = {}
        out["alias_merge"] = am

    reviews = [r for r in (am.get("review_candidates") or []) if isinstance(r, dict)]
    merged = [e for e in (am.get("merged_entities") or []) if isinstance(e, dict)]
    trash = [t for t in (am.get("trash_candidates") or []) if isinstance(t, dict)]

    decisions_raw = hitl.get("decisions") if isinstance(hitl, dict) else None
    decisions: dict[str, Any] = decisions_raw if isinstance(decisions_raw, dict) else {}

    keep: list[dict[str, Any]] = []
    for row in reviews:
        sig = review_row_signature(row)
        d = decisions.get(sig)
        if not isinstance(d, dict):
            keep.append(row)
            continue
        explicit = str(d.get("action") or "").lower().strip()
        if explicit == "approved":
            merged.append(_merged_entity_from_approved(row))
        elif explicit == "rejected":
            trash.append(_trash_from_rejected(row))
        elif "queue" in row and "action" not in row:
            resolved = decision_from_queue(str(row.get("queue") or ""))
            d = {**d, "resolved_from_queue": resolved}
            if resolved == "bulk_approved":
                merged.append(_merged_entity_from_approved(row))
            elif resolved == "blocked":
                trash.append(_trash_from_rejected(row))
            elif resolved == "review_required":
                keep.append(row)
            else:
                keep.append(row)
        else:
            keep.append(row)

    am["review_candidates"] = keep
    am["merged_entities"] = merged
    am["trash_candidates"] = trash
    return out


def load_hitl_sidecar(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "decisions": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "decisions": {}}
    return raw if isinstance(raw, dict) else {"version": 1, "decisions": {}}
