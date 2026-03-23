"""Broken lineage (manifest-unknown co-query endpoints) in AutonomousPlanner."""

from __future__ import annotations

from ama.ddl_manifest import normalize_manifest_table_key
from ama.planner import AutonomousPlanner
from ama.planner.broken_lineage import compute_planner_breakage, enrich_lineage_payload


def test_enrich_lineage_payload_marks_unknown_tables() -> None:
    lineage = {
        "edges": [
            {"from": "hr.employees", "to": "ghost_system.hr_external_logs", "weight": 2},
        ],
    }
    manifest = {"hr.employees": "ddl/hr_employees.json"}
    out = enrich_lineage_payload(lineage, manifest)
    assert "ghost_system.hr_external_logs" in (out.get("broken_table_keys") or [])


def test_compute_planner_breakage_flags_inventory_and_ghosts() -> None:
    mk = normalize_manifest_table_key("hr.employees")
    report = {
        "ddl_manifest_table_keys": [mk],
        "lineage": {
            "edges": [
                {"from": "hr.employees", "to": "ghost_system.hr_external_logs", "weight": 1},
            ],
        },
        "discovery": {
            "inventory": [
                {
                    "full_name": "hr.employees",
                    "business_domain": "HR",
                    "query_count": 10,
                    "priority_score": 50.0,
                },
            ],
        },
    }
    per, ghosts = compute_planner_breakage(report)
    assert per["hr.employees"]["is_broken"] is True
    assert "ghost_system.hr_external_logs" in per["hr.employees"]["missing_parents"]
    assert "ghost_system.hr_external_logs" in ghosts


def test_autonomous_planner_sets_is_broken_on_planned_tables() -> None:
    mk = normalize_manifest_table_key("hr.employees")
    report = {
        "migration_context": "hr.employees",
        "ddl_manifest_table_keys": [mk],
        "lineage": {
            "edges": [
                {"from": "hr.employees", "to": "ghost_system.hr_external_logs", "weight": 1},
            ],
        },
        "discovery": {
            "inventory": [
                {
                    "full_name": "hr.employees",
                    "business_domain": "HR",
                    "query_count": 10,
                    "priority_score": 50.0,
                    "status": "Ready",
                },
            ],
        },
    }
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=20)
    d = plan.to_dict()
    waves = d.get("waves") or []
    assert waves
    hr_wave = waves[0]
    tbl0 = (hr_wave.get("tables") or [{}])[0]
    assert tbl0.get("is_broken") is True
    assert tbl0.get("missing_parents")
    # Ghost placeholder wave
    assert any("manifest gaps" in str(w.get("name", "")).lower() for w in waves)
