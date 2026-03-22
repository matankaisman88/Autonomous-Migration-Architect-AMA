"""Tests for log_analysis, planner, data_quality, security."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ama.data_quality import run_dq_suite
from ama.log_analysis import LogAnalysisConfig, LogAnalysisEngine
from ama.planner import AutonomousPlanner
from ama.security import redact_path


def test_redact_path_shortens() -> None:
    s = redact_path(Path("C:/Users/someone/proj/data/x.json"), keep_segments=2)
    assert "..." in s or "data" in s


def test_run_dq_suite_minimal_ok() -> None:
    report = {
        "schema_version": "1.1",
        "ingestion_stats": {"total_rows": 1, "parse_ok": 1},
        "discovery": {"enabled": False},
        "target_table": "a.b",
        "columns": [],
    }
    r = run_dq_suite(report)
    assert r.ok


def test_plan_from_report_empty() -> None:
    plan = AutonomousPlanner().plan_from_report({"discovery": {"enabled": True, "inventory": []}})
    assert plan.waves == []
    assert any("No discovery inventory" in n for n in plan.notes)


def test_plan_from_report_waves() -> None:
    report = {
        "target_table": "sales.orders",
        "discovery": {
            "enabled": True,
            "target_full_table": "sales.orders",
            "executive_summary": {
                "domain_matrix": [
                    {
                        "business_domain": "CRM",
                        "business_importance": 55.0,
                        "migration_complexity": 40.0,
                        "narrative": "CRM narrative for tests.",
                    },
                    {
                        "business_domain": "Finance",
                        "business_importance": 80.0,
                        "migration_complexity": 60.0,
                        "narrative": "Finance narrative for tests.",
                    },
                ],
                "risk_hotspots": [{"table": "a.t1", "blast_radius_score": 42.0}],
            },
            "inventory": [
                {
                    "full_name": "a.t1",
                    "business_domain": "Finance",
                    "priority_score": 90.0,
                    "query_count": 10,
                    "status": "x",
                },
                {
                    "full_name": "a.t2",
                    "business_domain": "CRM",
                    "priority_score": 80.0,
                    "query_count": 5,
                    "status": "y",
                },
            ],
        },
    }
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=10, max_waves=10)
    assert len(plan.waves) >= 1
    for w in plan.waves:
        assert w.business_rationale.strip()
        assert w.technical_rationale.strip()
        assert "Finance" in w.business_rationale or "CRM" in w.business_rationale
        assert w.metrics.get("table_count") == len(w.tables)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "sample_data" / "sql_logs").is_dir(),
    reason="sample_data/sql_logs missing",
)
def test_log_analysis_engine_smoke() -> None:
    root = Path(__file__).resolve().parents[1]
    logs = list((root / "sample_data" / "sql_logs").glob("*.jsonl"))
    if not logs:
        pytest.skip("no jsonl fixtures")
    eng = LogAnalysisEngine(LogAnalysisConfig(env_filter=None, max_records_per_file=100))
    s = eng.analyze_paths([logs[0]])
    assert s.total_rows >= 1
    assert "parse_ok" in s.telemetry
