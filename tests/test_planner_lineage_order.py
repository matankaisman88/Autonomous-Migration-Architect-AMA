"""Planner lineage ordering (co-query DAG + topological sort)."""

from __future__ import annotations

from ama.planner import AutonomousPlanner
from ama.planner.lineage_order import migration_order_full_names


def test_migration_order_respects_lineage_pair() -> None:
    """Higher-priority (more-queried) table is the source and migrates first."""
    rows = [
        {
            "full_name": "finance.invoices",
            "business_domain": "Finance",
            "priority_score": 100.0,
            "query_count": 1830,
            "status": "",
        },
        {
            "full_name": "finance.payments",
            "business_domain": "Finance",
            "priority_score": 49.54,
            "query_count": 1100,
            "status": "",
        },
    ]
    report = {
        "lineage": {
            "edges": [
                {"from": "finance.invoices", "to": "finance.payments", "weight": 5, "kind": "coquery"},
            ],
        },
    }
    order, used = migration_order_full_names(rows, report)
    assert used is True
    assert order.index("finance.invoices") < order.index("finance.payments"), (
        f"Expected invoices before payments, got order: {order}"
    )


def test_plan_from_report_lineage_wave_order() -> None:
    report = {
        "discovery": {
            "enabled": True,
            "inventory": [
                {
                    "full_name": "finance.invoices",
                    "business_domain": "Finance",
                    "priority_score": 95.0,
                    "query_count": 1800,
                    "status": "",
                },
                {
                    "full_name": "finance.payments",
                    "business_domain": "Finance",
                    "priority_score": 60.0,
                    "query_count": 1100,
                    "status": "",
                },
            ],
        },
        "lineage": {
            "edges": [
                {"from": "finance.invoices", "to": "finance.payments", "weight": 3, "kind": "coquery"},
            ],
        },
    }
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=10, max_waves=10)
    assert len(plan.waves) == 1
    names = [t.full_name for t in plan.waves[0].tables]
    assert names.index("finance.invoices") < names.index("finance.payments"), (
        f"Expected invoices before payments, got: {names}"
    )
    assert any("lineage co-query" in n for n in plan.notes)


def test_no_lineage_falls_back_to_priority_desc() -> None:
    rows = [
        {"full_name": "a.x", "business_domain": "D", "priority_score": 10.0, "query_count": 0, "status": ""},
        {"full_name": "a.y", "business_domain": "D", "priority_score": 20.0, "query_count": 0, "status": ""},
    ]
    order, used = migration_order_full_names(rows, {})
    assert used is False
    assert order == ["a.y", "a.x"]
