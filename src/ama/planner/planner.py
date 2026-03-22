"""
Autonomous Planner — derives migration waves from discovery inventory and risk signals.
"""

from __future__ import annotations

from typing import Any

from ama.planner.models import MigrationPlan, MigrationWave, PlannedTable


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
        Build waves by sorting inventory rows by ``priority_score`` (desc) and grouping
        by ``business_domain`` into bounded waves.
        """
        disc = report.get("discovery") or {}
        inv = disc.get("inventory") if isinstance(disc.get("inventory"), list) else []
        target = str(disc.get("target_full_table") or report.get("target_table") or "")

        rows: list[dict[str, Any]] = [r for r in inv if isinstance(r, dict)]
        rows.sort(
            key=lambda r: (-float(r.get("priority_score") or 0.0), str(r.get("full_name", ""))),
        )

        by_domain: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            dom = str(r.get("business_domain") or "Unclassified")
            by_domain.setdefault(dom, []).append(r)

        plan = MigrationPlan(target_focus=target)
        wave_id = 0
        for domain, drs in sorted(by_domain.items(), key=lambda x: x[0].lower()):
            chunk: list[PlannedTable] = []
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
                if len(chunk) >= max_tables_per_wave:
                    wave_id += 1
                    if wave_id > max_waves:
                        plan.notes.append(f"Truncated after {max_waves} waves (cap).")
                        return plan
                    plan.waves.append(
                        MigrationWave(wave_id=wave_id, name=f"{domain} (part)", tables=chunk)
                    )
                    chunk = []
            if chunk:
                wave_id += 1
                if wave_id > max_waves:
                    plan.notes.append(f"Truncated after {max_waves} waves (cap).")
                    break
                plan.waves.append(
                    MigrationWave(wave_id=wave_id, name=domain, tables=chunk),
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
