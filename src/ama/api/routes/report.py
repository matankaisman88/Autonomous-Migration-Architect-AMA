from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ama.api import deps
from ama.migration_agent import agent_tools
from ama.ui.report_helpers import load_report_json

logger = logging.getLogger(__name__)
router = APIRouter()


class LoadReportRequest(BaseModel):
    path: str


class InventoryQuery(BaseModel):
    domain: str | None = None
    queue: str | None = None
    limit: int = Field(default=200, ge=1, le=5000)


@router.post("/load")
def load_report(body: LoadReportRequest) -> dict[str, Any]:
    """Load a report via report_helpers and cache it by deterministic report_id."""
    try:
        report_path = Path(body.path).expanduser().resolve()
        report = load_report_json(report_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("report load failed")
        raise HTTPException(status_code=500, detail=f"Failed to load report: {exc}") from exc

    inv = report.get("discovery", {}).get("inventory", [])
    domains = sorted(
        {
            str(row.get("business_domain") or "").strip()
            for row in inv
            if isinstance(row, dict) and str(row.get("business_domain") or "").strip()
        }
    )
    report_id = deps.make_report_id(report_path)
    deps.REPORT_STORE[report_id] = report
    deps.PATH_STORE[report_id] = report_path
    from ama.lineage import clear_lineage_adjacency_cache
    from ama.schema_relationships import clear_pk_fk_cache

    clear_lineage_adjacency_cache(report_id=report_id)
    clear_pk_fk_cache(report_id=report_id)
    return {"report_id": report_id, "table_count": len(inv), "domains": domains}


@router.get("/{report_id}/summary")
def report_summary(report_id: str) -> dict[str, Any]:
    """Return lightweight summary fields from the loaded report payload."""
    report = deps.get_report(report_id)
    inv = report.get("discovery", {}).get("inventory", [])
    domains = sorted(
        {
            str(row.get("business_domain") or "").strip()
            for row in inv
            if isinstance(row, dict) and str(row.get("business_domain") or "").strip()
        }
    )
    lineage_edges = report.get("lineage", {}).get("edges", [])
    has_glossary = bool(report.get("business_glossary") or report.get("glossary"))
    return {
        "report_id": report_id,
        "table_count": len(inv),
        "domains": domains,
        "migration_context": str(report.get("migration_context") or ""),
        "lineage_edge_count": len(lineage_edges) if isinstance(lineage_edges, list) else 0,
        "has_glossary": has_glossary,
    }


@router.get("/{report_id}/inventory")
def report_inventory(report_id: str, domain: str | None = None, queue: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Call query_inventory for filtered scored-table inventory rows."""
    report = deps.get_report(report_id)
    q = InventoryQuery(domain=domain, queue=queue, limit=limit)
    filters: dict[str, Any] = {}
    if q.domain:
        filters["domain"] = q.domain
    if q.queue:
        filters["queue"] = q.queue
    try:
        res = agent_tools.query_inventory(report=report, filters=filters, limit=q.limit)
        return res.tables
    except Exception as exc:
        logger.exception("inventory query failed")
        raise HTTPException(status_code=500, detail=f"Inventory query failed: {exc}") from exc

