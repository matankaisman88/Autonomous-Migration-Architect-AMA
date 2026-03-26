from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ama.api import deps
from ama.data_quality import run_dq_suite

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/{report_id}/run")
def run_dq(report_id: str) -> dict[str, Any]:
    """Run DQ suite for a loaded report and return serializable check results."""
    report = deps.get_report(report_id)
    try:
        result = run_dq_suite(report)
        checks = [c.to_dict() for c in result.checks]
        return {"ok": result.ok, "checks": checks}
    except Exception as exc:
        logger.exception("dq run failed")
        raise HTTPException(status_code=500, detail=f"DQ run failed: {exc}") from exc

