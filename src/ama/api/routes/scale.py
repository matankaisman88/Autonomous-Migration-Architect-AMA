from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ama.api import deps
from ama.migration_agent import agent_tools
from ama.scale_engine import evaluate_batch

logger = logging.getLogger(__name__)
router = APIRouter()


class EvaluateRequest(BaseModel):
    conf_floor: int = 70
    crit_ceil: int = 40
    dry_run: bool = True


@router.post("/{report_id}/evaluate")
def scale_evaluate(report_id: str, body: EvaluateRequest) -> dict[str, Any]:
    """Call evaluate_batch and expose queue counts plus scored-table details."""
    report = deps.get_report(report_id)
    try:
        res = evaluate_batch(
            report=report,
            dry_run=body.dry_run,
            conf_floor=body.conf_floor,
            crit_ceil=body.crit_ceil,
        )
    except Exception as exc:
        logger.exception("scale evaluate failed")
        raise HTTPException(status_code=500, detail=f"Scale evaluation failed: {exc}") from exc

    scored_tables = [
        {
            "table_key": s.table_key,
            "queue": s.queue,
            "confidence": s.confidence,
            "criticality": s.criticality,
            "business_domain": s.business_domain,
            "confidence_reason": s.confidence_result.reason,
            "criticality_reason": s.criticality_result.reason,
            "anomaly_flags": [{"level": f.level, "name": f.name, "reason": f.reason} for f in s.anomaly_flags],
        }
        for s in res.scored_tables
    ]
    return {
        "would_migrate": res.would_migrate,
        "would_flag_review": res.would_flag_review,
        "would_block": res.would_block,
        "threshold_used": res.threshold_used,
        "contract_preview": {
            "rules": res.contract_preview.rules,
            "contract_id": res.contract_preview.contract_id,
            "excluded": res.contract_preview.excluded,
            "table_count": res.contract_preview.table_count,
        },
        "scored_tables": scored_tables,
    }


@router.get("/{report_id}/explain/{table_key:path}")
def scale_explain(report_id: str, table_key: str) -> dict[str, Any]:
    """Call explain_table_score for a full confidence/criticality breakdown."""
    report = deps.get_report(report_id)
    try:
        res = agent_tools.explain_table_score(report=report, table_key=table_key)
    except Exception as exc:
        logger.exception("explain failed")
        raise HTTPException(status_code=500, detail=f"Explain failed: {exc}") from exc
    if str(res.summary).startswith(f"{table_key} is missing from inventory"):
        raise HTTPException(status_code=404, detail="table not found")
    return {
        "table_key": res.table_key,
        "queue": res.queue,
        "confidence": {
            "score": res.confidence.score,
            "reason": res.confidence.reason,
            "components": res.confidence.components,
        },
        "criticality": {
            "score": res.criticality.score,
            "reason": res.criticality.reason,
            "components": res.criticality.components,
        },
        "anomaly_flags": [{"level": f.level, "name": f.name, "reason": f.reason} for f in res.anomaly_flags],
        "summary": res.summary,
    }

