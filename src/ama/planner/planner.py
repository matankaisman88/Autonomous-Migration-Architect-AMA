"""
Autonomous Planner — derives migration waves from discovery inventory and risk signals.
"""

from __future__ import annotations

from typing import Any

from ama.planner.lineage_order import sort_rows_by_migration_order
from ama.planner.models import MigrationPlan, MigrationWave, PlannedTable
from ama.planner.rationale import build_wave_rationales, enrich_planned_tables


class AutonomousPlanner:
    """
    Orchestration layer for migration planning.

    Consumes an AMA **report** dict (JSON export) and emits a :class:`MigrationPlan`.
    """

    def plan_from_report(
        self,
        report: dict[str, Any],
        *,
        max_tables_per_wave: int = 25,
        max_waves: int = 20,
    ) -> MigrationPlan:
        """
        Build waves by grouping ``business_domain`` into bounded waves.

        Table order within and across domains follows **lineage** co-query edges when present
        (see :mod:`ama.planner.lineage_order`); otherwise **priority_score** descending.
        """
        disc = report.get("discovery") or {}
        inv = disc.get("inventory") if isinstance(disc.get("inventory"), list) else []
        target = str(disc.get("target_full_table") or report.get("target_table") or "")

        rows: list[dict[str, Any]] = [r for r in inv if isinstance(r, dict)]
        rows, lineage_used = sort_rows_by_migration_order(rows, report)

        by_domain: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            dom = str(r.get("business_domain") or "Unclassified")
            by_domain.setdefault(dom, []).append(r)

        pos: dict[str, int] = {}
        for i, r in enumerate(rows):
            fn = str(r.get("full_name") or "").strip()
            if fn:
                pos[fn] = i

        def _domain_wave_key(dom: str) -> tuple[int, str]:
            drs = by_domain.get(dom) or []
            if not drs:
                return (10**12, dom.lower())
            earliest = min(pos.get(str(r.get("full_name") or "").strip(), 10**9) for r in drs)
            return (earliest, dom.lower())

        plan = MigrationPlan(target_focus=target)
        if lineage_used:
            plan.notes.append(
                "Inventory order respects lineage co-query edges (DAG over inventory, priority tie-break).",
            )
        wave_id = 0
        domain_sort = (
            (lambda items: sorted(items, key=lambda x: _domain_wave_key(x[0])))
            if lineage_used
            else (lambda items: sorted(items, key=lambda x: x[0].lower()))
        )
        for domain, drs in domain_sort(by_domain.items()):
            chunk: list[PlannedTable] = []
            chunk_rows: list[dict[str, Any]] = []
            domain_emitted_partial = False
            for r in drs:
                fn = str(r.get("full_name") or "")
                if not fn:
                    continue
                pt = PlannedTable(
                    full_name=fn,
                    business_domain=domain,
                    priority_score=float(r.get("priority_score") or 0.0),
                    query_count=int(r.get("query_count") or 0),
                    rationale=str(r.get("status") or ""),
                )
                chunk.append(pt)
                chunk_rows.append(r)
                if len(chunk) >= max_tables_per_wave:
                    domain_emitted_partial = True
                    wave_id += 1
                    if wave_id > max_waves:
                        plan.notes.append(f"Truncated after {max_waves} waves (cap).")
                        return plan
                    wname = f"{domain} (part)"
                    plan.waves.append(
                        self._wave_with_rationale(
                            wave_id=wave_id,
                            name=wname,
                            domain=domain,
                            chunk=chunk,
                            chunk_rows=chunk_rows,
                            report=report,
                            is_partial_wave=True,
                            max_tables_per_wave=max_tables_per_wave,
                        ),
                    )
                    chunk = []
                    chunk_rows = []
            if chunk:
                wave_id += 1
                if wave_id > max_waves:
                    plan.notes.append(f"Truncated after {max_waves} waves (cap).")
                    break
                wname = domain
                plan.waves.append(
                    self._wave_with_rationale(
                        wave_id=wave_id,
                        name=wname,
                        domain=domain,
                        chunk=chunk,
                        chunk_rows=chunk_rows,
                        report=report,
                        is_partial_wave=domain_emitted_partial,
                        max_tables_per_wave=max_tables_per_wave,
                    ),
                )

        es = disc.get("executive_summary") or {}
        rh = es.get("risk_hotspots") or []
        if isinstance(rh, list) and rh:
            plan.notes.append(
                "Risk hotspots present in report — review blast_radius_score before scheduling.",
            )
        if not plan.waves:
            plan.notes.append(
                "No discovery inventory in report — run `ama-ingest run --discovery-mode` to populate.",
            )
        return plan

    @staticmethod
    def _wave_with_rationale(
        *,
        wave_id: int,
        name: str,
        domain: str,
        chunk: list[PlannedTable],
        chunk_rows: list[dict[str, Any]],
        report: dict[str, Any],
        is_partial_wave: bool,
        max_tables_per_wave: int,
    ) -> MigrationWave:
        enriched = enrich_planned_tables(chunk, chunk_rows, report)
        br, tr, metrics = build_wave_rationales(
            domain=domain,
            planned_tables=enriched,
            inv_rows=chunk_rows,
            report=report,
            is_partial_wave=is_partial_wave,
            max_tables_per_wave=max_tables_per_wave,
        )
        return MigrationWave(
            wave_id=wave_id,
            name=name,
            tables=enriched,
            business_rationale=br,
            technical_rationale=tr,
            metrics=metrics,
        )
