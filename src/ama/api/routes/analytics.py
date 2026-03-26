from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ama.api import deps
from ama.business_logic import (
    build_business_glossary_entries,
    build_impact_readiness_scatter_rows,
    domain_data_health_filtered,
    group_glossary_entries,
    semantic_concept_search,
)
from ama.ui.report_helpers import (
    _merge_rows_for_filters,
    filter_glossary_grouped,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{report_id}/glossary")
def analytics_glossary(
    report_id: str,
    conf_min: float = Query(default=0.0, ge=0.0, le=1.0),
    portfolio: str = "All",
    domains: str = "",
) -> dict[str, Any]:
    """Build and filter business glossary cards using existing business/report helpers."""
    report = deps.get_report(report_id)
    try:
        domain_list = [d.strip() for d in domains.split(",") if d.strip()]
        merged, review, trash = _merge_rows_for_filters(
            report,
            domains=domain_list or None,
            portfolio=portfolio,
            conf_min=conf_min,
        )
        all_entries = build_business_glossary_entries(report)
        grouped = group_glossary_entries(all_entries)
        filtered = filter_glossary_grouped(
            grouped,
            report=report,
            domains=domain_list or None,
            portfolio=portfolio,
            conf_min=conf_min,
        )
        return {
            "entries": filtered,
            "counts": {"merged": len(merged), "review": len(review), "trash": len(trash)},
        }
    except Exception as exc:
        logger.exception("glossary analytics failed")
        raise HTTPException(status_code=500, detail=f"Glossary analytics failed: {exc}") from exc


@router.get("/{report_id}/domain-health")
def analytics_domain_health(
    report_id: str,
    conf_min: float = Query(default=0.0, ge=0.0, le=1.0),
    portfolio: str = "All",
    domains: str = "",
) -> dict[str, Any]:
    """Compute per-domain health metrics with the same filtered-bucket logic as dashboard."""
    report = deps.get_report(report_id)
    try:
        domain_list = [d.strip() for d in domains.split(",") if d.strip()]
        merged, review, trash = _merge_rows_for_filters(
            report,
            domains=domain_list or None,
            portfolio=portfolio,
            conf_min=conf_min,
        )
        inv = (report.get("discovery") or {}).get("inventory") or []
        dom_to_tables: dict[str, set[str]] = {}
        for row in inv:
            if not isinstance(row, dict):
                continue
            dom = str(row.get("business_domain") or "").strip()
            fn = str(row.get("full_name") or "").strip()
            if not dom or not fn:
                continue
            dom_to_tables.setdefault(dom, set()).add(fn)
        results = [
            domain_data_health_filtered(
                report,
                dom,
                merged_all=merged,
                review_all=review,
                trash_all=trash,
                inventory_full_names_for_domain=tables,
            )
            for dom, tables in sorted(dom_to_tables.items())
            if (not domain_list or dom in domain_list)
        ]
        return {"domains": results}
    except Exception as exc:
        logger.exception("domain health analytics failed")
        raise HTTPException(status_code=500, detail=f"Domain health analytics failed: {exc}") from exc


@router.get("/{report_id}/semantic-search")
def analytics_semantic_search(report_id: str, q: str) -> dict[str, Any]:
    """Run semantic concept search over report-derived glossary/table/column content."""
    report = deps.get_report(report_id)
    if not str(q).strip():
        raise HTTPException(status_code=400, detail="query is required")
    try:
        return semantic_concept_search(report, q)
    except Exception as exc:
        logger.exception("semantic search failed")
        raise HTTPException(status_code=500, detail=f"Semantic search failed: {exc}") from exc


@router.get("/{report_id}/impact-scatter")
def analytics_impact_scatter(report_id: str) -> dict[str, Any]:
    """Return impact-readiness scatter points used by overview analytics charts."""
    report = deps.get_report(report_id)
    try:
        return {"rows": build_impact_readiness_scatter_rows(report)}
    except Exception as exc:
        logger.exception("impact scatter failed")
        raise HTTPException(status_code=500, detail=f"Impact scatter failed: {exc}") from exc

