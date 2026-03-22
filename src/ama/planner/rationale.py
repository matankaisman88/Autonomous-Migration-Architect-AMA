"""
Short, wave-specific rationales (report-driven — no generic boilerplate).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ama.planner.models import PlannedTable


def _exec_summary(report: dict[str, Any]) -> dict[str, Any]:
    disc = report.get("discovery") or {}
    es = disc.get("executive_summary")
    return es if isinstance(es, dict) else {}


def lookup_domain_matrix_row(domain: str, report: dict[str, Any]) -> dict[str, Any] | None:
    for row in _exec_summary(report).get("domain_matrix") or []:
        if isinstance(row, dict) and str(row.get("business_domain") or "") == domain:
            return row
    return None


def _fact_sheet_by_name(report: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _exec_summary(report).get("table_fact_sheets") or []:
        if not isinstance(row, dict):
            continue
        fn = str(row.get("full_qualified_name") or "").strip()
        desc = str(row.get("business_description") or "").strip()
        if fn and desc:
            out[fn] = desc
    return out


def merge_entity_count_for_table(table: str, report: dict[str, Any]) -> int:
    n = 0
    for e in (report.get("alias_merge") or {}).get("merged_entities") or []:
        if isinstance(e, dict) and str(e.get("source_table") or "") == table:
            n += 1
    return n


def risk_hotspots_for_tables(table_names: set[str], report: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for h in _exec_summary(report).get("risk_hotspots") or []:
        if isinstance(h, dict) and str(h.get("table") or "") in table_names:
            hits.append(h)
    return hits


def enrich_planned_tables(
    tables: list[PlannedTable],
    inv_rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> list[PlannedTable]:
    """Return new PlannedTable list with business_context and technical_note filled from report."""
    fs = _fact_sheet_by_name(report)
    enriched: list[PlannedTable] = []
    for pt, inv in zip(tables, inv_rows):
        bctx = fs.get(pt.full_name, "").strip()
        if not bctx:
            bctx = str(inv.get("business_description") or "").strip()
        if len(bctx) > 280:
            bctx = bctx[:277] + "..."
        mc = merge_entity_count_for_table(pt.full_name, report)
        tech_parts: list[str] = []
        st = str(inv.get("status") or pt.rationale or "").strip()
        if st:
            tech_parts.append(st)
        if mc:
            tech_parts.append(f"{mc} merge cluster(s)")
        tech = " · ".join(tech_parts)
        enriched.append(
            replace(pt, business_context=bctx, technical_note=tech),
        )
    return enriched


def _fmt_priority(score: float) -> str:
    """Two decimal places for display (e.g. 4.12)."""
    return f"{float(score):.2f}"


def _wave_run_order(planned_tables: list[PlannedTable]) -> str:
    ordered = sorted(planned_tables, key=lambda p: (-p.priority_score, p.full_name.lower()))
    if len(ordered) <= 6:
        return " → ".join(f"`{p.full_name}` ({_fmt_priority(p.priority_score)})" for p in ordered)
    head = ordered[:4]
    tail = len(ordered) - 4
    return " → ".join(f"`{p.full_name}` ({_fmt_priority(p.priority_score)})" for p in head) + f" → +{tail} more"


def build_wave_rationales(
    *,
    domain: str,
    planned_tables: list[PlannedTable],
    inv_rows: list[dict[str, Any]],
    report: dict[str, Any],
    is_partial_wave: bool,
    max_tables_per_wave: int,
) -> tuple[str, str, dict[str, Any]]:
    """
    Two short paragraphs: business (why this slice matters) and technical (this wave’s concrete shape).
    """
    n = len(planned_tables)
    names = {pt.full_name for pt in planned_tables}
    total_q = sum(int(r.get("query_count") or 0) for r in inv_rows if isinstance(r, dict))
    avg_pri = (
        sum(float(pt.priority_score) for pt in planned_tables) / max(n, 1) if planned_tables else 0.0
    )
    max_pri = max((float(pt.priority_score) for pt in planned_tables), default=0.0)
    min_pri = min((float(pt.priority_score) for pt in planned_tables), default=0.0)

    dm = lookup_domain_matrix_row(domain, report)
    risk_hits = risk_hotspots_for_tables(names, report)
    merge_pairs = [
        (pt.full_name, merge_entity_count_for_table(pt.full_name, report))
        for pt in planned_tables
        if merge_entity_count_for_table(pt.full_name, report) > 0
    ]

    disc = report.get("discovery") or {}
    target_focus = str(disc.get("target_full_table") or report.get("target_table") or "")
    merge_scope = report.get("merge_scope") if isinstance(report.get("merge_scope"), dict) else {}

    fs = _fact_sheet_by_name(report)
    documented = [pt.full_name for pt in planned_tables if pt.full_name in fs]

    # --- Business: one tight paragraph, numbers + this domain only ---
    biz_bits: list[str] = []
    biz_bits.append(
        f"**{domain}** — {n} table(s), **{total_q}** logged queries, avg priority **{_fmt_priority(avg_pri)}%** "
        f"(spread **{_fmt_priority(min_pri)}%–{_fmt_priority(max_pri)}%**)."
    )
    if dm:
        imp = dm.get("business_importance")
        cx = dm.get("migration_complexity")
        biz_bits.append(f"vs other domains in this report: importance **{imp}%**, complexity **{cx}%**.")
    if documented:
        if len(documented) <= 4:
            biz_bits.append(f"Exec descriptions on: {', '.join(f'`{x}`' for x in documented)}.")
        else:
            biz_bits.append(f"Exec descriptions on **{len(documented)}** tables in this wave.")
    if risk_hits:
        rh = ", ".join(
            f"`{h.get('table')}` (blast {float(h.get('blast_radius_score') or 0):.0f})" for h in risk_hits[:5]
        )
        if len(risk_hits) > 5:
            rh += f", +{len(risk_hits) - 5}"
        biz_bits.append(f"High lineage blast in wave: {rh}.")
    if is_partial_wave:
        biz_bits.append(f"Split wave — domain has more than **{max_tables_per_wave}** tables; this is one chunk.")

    business = " ".join(biz_bits)

    # --- Technical: ordering, merge, risk, target — only what applies ---
    tech_bits: list[str] = []
    tech_bits.append(f"**Priority order in this wave:** {_wave_run_order(planned_tables)}.")
    if merge_pairs:
        mp = ", ".join(f"`{fn}` ({c})" for fn, c in merge_pairs[:6])
        if len(merge_pairs) > 6:
            mp += f", +{len(merge_pairs) - 6}"
        tech_bits.append(f"**Alias merge:** {mp} cluster(s).")
    if risk_hits:
        tech_bits.append("Validate **lineage dependency order** before cutover for those hotspot(s).")
    if target_focus and target_focus in names:
        tech_bits.append(f"Includes report **target** `{target_focus}`.")
    if merge_scope:
        raw_keys = merge_scope.get("table_names_merged") or []
        key_set = {str(x).strip() for x in raw_keys if str(x).strip()}
        overlap = sorted(names & key_set)
        if overlap:
            listed = ", ".join(f"`{x}`" for x in overlap[:6])
            if len(overlap) > 6:
                listed += f", +{len(overlap) - 6}"
            tech_bits.append(f"**DDL merge** ran on tables in this wave: {listed}.")

    technical = " ".join(tech_bits)

    metrics: dict[str, Any] = {
        "table_count": n,
        "total_query_count": total_q,
        "avg_priority_score": round(avg_pri, 2),
        "priority_min": round(min_pri, 2),
        "priority_max": round(max_pri, 2),
        "domain_matrix_importance": dm.get("business_importance") if dm else None,
        "domain_matrix_complexity": dm.get("migration_complexity") if dm else None,
        "risk_hotspot_hits": len(risk_hits),
        "tables_with_merge_entities": len(merge_pairs),
    }

    return business, technical, metrics
