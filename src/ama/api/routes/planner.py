from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ama.api import deps
from ama.planner import AutonomousPlanner

logger = logging.getLogger(__name__)
router = APIRouter()


class PlannerRequest(BaseModel):
    max_tables_per_wave: int = Field(default=25, ge=1, le=200)
    max_waves: int = Field(default=20, ge=1, le=200)


@router.post("/{report_id}/waves")
def planner_waves(report_id: str, body: PlannerRequest) -> dict[str, Any]:
    """Call AutonomousPlanner.plan_from_report and return JSON-serializable wave output."""
    report = deps.get_report(report_id)
    try:
        plan = AutonomousPlanner().plan_from_report(
            report,
            max_tables_per_wave=body.max_tables_per_wave,
            max_waves=body.max_waves,
        )
        return {
            "migration_context": plan.migration_context,
            "notes": plan.notes,
            "waves": [w.to_dict() for w in plan.waves],
        }
    except Exception as exc:
        logger.exception("planner generation failed")
        raise HTTPException(status_code=500, detail=f"Planner generation failed: {exc}") from exc

