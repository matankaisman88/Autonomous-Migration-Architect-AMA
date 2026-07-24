from __future__ import annotations

import json
from pathlib import Path

from ama.migration_agent import agent_tools
from ama.scale_engine import evaluate_batch
from ama.scale_engine.audit import append_decision
from ama.scale_engine.contract import build_contract
from ama.scale_engine.anomaly import AnomalyFlag
from ama.scale_engine.criticality import CriticalityResult
from ama.scale_engine.scorer import ConfidenceResult, score_confidence


def _base_report() -> dict:
    inv = []
    for i in range(10):
        inv.append(
            {
                "full_name": f"finance.t{i}",
                "business_domain": "Finance",
                "query_count": 5,
                "status": "active",
                "priority_score": 0.5,
                "column_count": 10,
            }
        )
    inv.append(
        {
            "full_name": "finance.outlier",
            "business_domain": "Finance",
            "query_count": 5,
            "status": "active",
            "priority_score": 0.5,
            "column_count": 30,
        }
    )
    importance = []
    for row in inv:
        col_n = int(row["column_count"])
        for idx in range(col_n):
            importance.append({"source_table": row["full_name"], "column": f"col_{idx}"})
    return {"discovery": {"inventory": inv}, "alias_merge": {}, "lineage": {"edges": []}, "importance_ddl": importance}


def test_column_count_outlier_warn_flag() -> None:
    report = _base_report()
    out = evaluate_batch(report=report, dry_run=True)
    by_table = {s.table_key: s for s in out.scored_tables}
    outlier = by_table["finance.outlier"]
    assert any(f.level == "WARN" and f.name == "column_count_outlier" for f in outlier.anomaly_flags)
    non_outliers = [k for k in by_table if k != "finance.outlier"]
    assert all(not any(f.name == "column_count_outlier" for f in by_table[k].anomaly_flags) for k in non_outliers)


def test_criticality_score_composition_forces_red() -> None:
    report = _base_report()
    report["discovery"]["inventory"] = [
        {
            "full_name": "finance.invoices",
            "business_domain": "Finance",
            "query_count": 500,
            "status": "active",
            "priority_score": 1.0,
            "column_count": 5,
        }
    ]
    report["lineage"]["edges"] = [
        {"source": "finance.invoices", "target": "a"},
        {"source": "finance.invoices", "target": "b"},
        {"source": "finance.invoices", "target": "c"},
    ]
    report["importance_ddl"] = [{"source_table": "finance.invoices", "column": f"invoice_col_{i}"} for i in range(5)]
    out = evaluate_batch(report=report, dry_run=True)
    scored = out.scored_tables[0]
    assert scored.criticality >= 80
    assert scored.queue == "red"


def test_non_sensitive_high_usage_with_single_downstream_can_stay_green() -> None:
    report = _base_report()
    report["discovery"]["inventory"] = [
        {
            "full_name": "sales.customers",
            "business_domain": "Sales",
            "query_count": 200,
            "status": "active",
            "priority_score": 1.0,
            "column_count": 6,
        }
    ]
    report["alias_merge"] = {
        "customer_id": "customer_id",
        "created_at": "created_at",
        "segment": "segment",
        "city": "city",
    }
    report["lineage"]["edges"] = [{"source": "sales.customers", "target": "sales.orders"}]
    report["importance_ddl"] = [
        {"source_table": "sales.customers", "column": "customer_id"},
        {"source_table": "sales.customers", "column": "created_at"},
        {"source_table": "sales.customers", "column": "segment"},
        {"source_table": "sales.customers", "column": "city"},
    ]
    out = evaluate_batch(report=report, dry_run=True)
    scored = out.scored_tables[0]
    assert scored.criticality < 40
    assert scored.queue == "green"


def test_high_usage_single_downstream_safe_table_at_500_queries() -> None:
    report = _base_report()
    report["discovery"]["inventory"] = [
        {
            "full_name": "sales.customers",
            "business_domain": "Sales",
            "query_count": 500,
            "status": "active",
            "priority_score": 1.0,
            "column_count": 6,
        }
    ]
    report["alias_merge"] = {
        "customer_id": "customer_id",
        "created_at": "created_at",
        "segment": "segment",
        "city": "city",
    }
    report["lineage"]["edges"] = [{"source": "sales.customers", "target": "sales.orders"}]
    report["importance_ddl"] = [
        {"source_table": "sales.customers", "column": "customer_id"},
        {"source_table": "sales.customers", "column": "created_at"},
        {"source_table": "sales.customers", "column": "segment"},
        {"source_table": "sales.customers", "column": "city"},
    ]
    out = evaluate_batch(report=report, dry_run=True)
    scored = out.scored_tables[0]
    # 500 queries alone is now enough usage-criticality to require review, even with a single safe downstream.
    assert scored.criticality >= 40
    assert scored.queue == "yellow"


def test_table_outside_manifest_scope_is_blocked() -> None:
    report = _base_report()
    report["discovery"]["inventory"] = [
        {
            "full_name": "legacy_hebrew.חשבוניות",
            "business_domain": "Legacy",
            "query_count": 30,
            "status": "active",
            "priority_score": 0.2,
            "column_count": 4,
        }
    ]
    report["ddl_manifest_table_keys"] = ["dbo.orders", "dbo.customers"]
    report["importance_ddl"] = [{"source_table": "legacy_hebrew.חשבוניות", "column": "מספר_חשבונית"}]
    out = evaluate_batch(report=report, dry_run=True)
    scored = out.scored_tables[0]
    assert scored.queue == "red"
    assert any(f.name == "outside_manifest_scope" and f.level == "BLOCK" for f in scored.anomaly_flags)


def test_contract_hash_stability() -> None:
    green = [
        {
            "table_key": "a",
            "confidence_components": {"glossary_match": 60, "type_pattern": 30},
            "criticality_components": {"lineage": 0, "usage": 0},
        }
    ]
    all_rows = [{"table_key": "a"}, {"table_key": "b"}]
    c1 = build_contract(green_rows=green, all_rows=all_rows)
    c2 = build_contract(green_rows=green, all_rows=all_rows)
    assert c1.contract_id == c2.contract_id
    green2 = [
        {
            "table_key": "a",
            "confidence_components": {"glossary_match": 61, "type_pattern": 30},
            "criticality_components": {"lineage": 0, "usage": 0},
        }
    ]
    c3 = build_contract(green_rows=green2, all_rows=all_rows)
    assert c1.contract_id != c3.contract_id


def test_audit_fail_safe(monkeypatch) -> None:
    class BadPath:
        def __truediv__(self, _other: str) -> "BadPath":
            return self

        @property
        def parent(self) -> "BadPath":
            return self

        def mkdir(self, *args, **kwargs) -> None:
            raise OSError("readonly")

        def open(self, *args, **kwargs):
            raise OSError("readonly")

    monkeypatch.setattr("ama.scale_engine.audit.project_root", lambda: BadPath())
    append_decision(
        table_key="x",
        decision="blocked",
        confidence=ConfidenceResult(score=0, reason="r", components={}),
        criticality=CriticalityResult(score=0, reason="r", components={}),
        anomaly_flags=[],
        contract_id="c",
        approved_by="u",
        approved_at="2026-01-01T00:00:00Z",
    )


def test_dry_run_does_not_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    report = _base_report()
    _ = evaluate_batch(report=report, dry_run=True)
    assert not (tmp_path / "audit_trail.jsonl").exists()


def test_query_inventory_filter_correctness() -> None:
    report = _base_report()
    out = agent_tools.query_inventory(report=report, filters={"queue": "green", "domain": "Finance"})
    assert all(t["queue"] == "green" and t["business_domain"] == "Finance" for t in out.tables)


def test_bulk_migrate_tables_dry_run_default(monkeypatch, tmp_path: Path) -> None:
    report = _base_report()
    called = {"write": 0}

    def _fake_write(*args, **kwargs):
        called["write"] += 1
        return {}

    monkeypatch.setattr("ama.migration_agent.agent_tools.request_write_permission", _fake_write)
    out = agent_tools.bulk_migrate_tables(
        report=report,
        report_path=tmp_path / "report.json",
        filters={"queue": "green"},
        dialect="duckdb",
        glossary_path=None,
    )
    assert out.dry_run is True
    assert called["write"] == 0


def test_explain_table_score_completeness() -> None:
    report = _base_report()
    out = agent_tools.explain_table_score(report=report, table_key="finance.t0")
    assert out.confidence.reason.strip()
    assert out.criticality.reason.strip()
    assert out.summary.strip()
    assert all(f.reason.strip() for f in out.anomaly_flags)


def test_tables_tab_score_columns_distinct_metrics() -> None:
    """Migration confidence (scale engine) is not `merge_confidence` as a percentage."""
    report = _base_report()
    report["alias_merge"] = {
        "merged_entities": [
            {
                "source_table": "finance.t0",
                "canonical_column": "col_0",
                "merge_confidence": 0.99,
                "source_columns": ["x"],
            }
        ]
    }
    row = next(s for s in evaluate_batch(report=report, dry_run=True).scored_tables if s.table_key == "finance.t0")
    assert row.confidence != int(round(0.99 * 100))


def test_explain_matches_evaluate_queue_for_same_thresholds() -> None:
    from ama.migration_agent.agent_tools import explain_table_score

    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "dbo.order_lines",
                    "business_domain": "CRM",
                    "query_count": 100,
                    "column_count": 7,
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "dbo.order_lines",
                    "canonical_column": "order_id",
                    "merge_confidence": 0.95,
                    "source_columns": ["order_id"],
                }
            ],
            "review_candidates": [],
            "trash_candidates": [],
            "ddl_manifest": None,
        },
        "ddl_manifest_table_keys": ["dbo.order_lines"],
        "importance_ddl": [
            {"source_table": "dbo.order_lines", "column": f"col_{i}", "data_type": "int"} for i in range(7)
        ],
        "lineage": {"edges": []},
    }
    batch = evaluate_batch(report=report, dry_run=True, conf_floor=70, crit_ceil=40)
    row = next(s for s in batch.scored_tables if s.table_key == "dbo.order_lines")
    explained = explain_table_score(report=report, table_key="dbo.order_lines")
    assert explained.queue == row.queue


def test_hitl_rejected_mapping_flags_table_yellow() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "logistics.shipments",
                    "business_domain": "Logistics",
                    "query_count": 10,
                    "column_count": 4,
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "logistics.shipments",
                    "canonical_column": "order_id",
                    "merge_confidence": 0.95,
                    "source_columns": ["order_id"],
                }
            ],
            "review_candidates": [],
            "trash_candidates": [
                {
                    "legacy_name": "shipmentid",
                    "suggested_ddl": "shipment_id",
                    "merge_confidence": 0.41,
                    "category": "hitl_rejected",
                    "source_table": "logistics.shipments",
                }
            ],
            "ddl_manifest": None,
        },
        "ddl_manifest_table_keys": ["logistics.shipments"],
        "importance_ddl": [
            {"source_table": "logistics.shipments", "column": "shipment_id", "data_type": "int"},
            {"source_table": "logistics.shipments", "column": "order_id", "data_type": "int"},
        ],
        "lineage": {"edges": []},
    }
    row = next(
        s for s in evaluate_batch(report=report, dry_run=True).scored_tables if s.table_key == "logistics.shipments"
    )
    assert row.queue != "green"
    assert any(f.name == "hitl_rejected_mapping" for f in row.anomaly_flags)


def test_bulk_migration_tab_dry_run_banner_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "DRY RUN MODE — no files will be written" in text
    assert "Dry Run Selected" in text
    start = text.index("### 🔴 Blocked")
    end = text.index('with st.expander("Audit Log Viewer"', start)
    blocked_chunk = text[start:end]
    assert "Explanation —" in blocked_chunk
    assert 'st.expander(f"Explanation —' in blocked_chunk
    assert "explain_table_score" in blocked_chunk
    assert "st.info(f\"{s.confidence_result.reason}" not in blocked_chunk


def test_tab_group_structure() -> None:
    from ama.ui.dashboard import ANALYSIS_TABS, EXECUTION_TABS

    assert "Bulk Migration" in ANALYSIS_TABS
    assert "Overview" in ANALYSIS_TABS
    assert "Tables" in ANALYSIS_TABS
    assert "Migration Agent" in EXECUTION_TABS
    assert "HITL Review" in EXECUTION_TABS
    overlap = set(ANALYSIS_TABS) & set(EXECUTION_TABS)
    assert overlap == set(), f"Tabs appear in both groups: {overlap}"
    assert "Bulk Migration" not in EXECUTION_TABS
    assert "Migration Agent" not in ANALYSIS_TABS


def test_launchpad_banner_renders_on_load_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "Migration Launchpad" in text
    assert "Go to Tables →" in text
    assert "Go to Bulk Migration →" in text
    assert "launchpad_expanded" in text


def test_tables_actions_buttons_for_queue_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "▶ Migrate" in text
    assert "👁 Review" in text
    assert "🔍 Explain" in text
    assert "▶ Migrate (after review)" in text
    assert "Medium confidence — review required before migrating." in text
    assert "Blocked — see explanation for details." in text


def test_tables_bulk_callout_banner_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "tables are ready for bulk approval" in text
    assert "Go to Bulk Migration →" in text


def test_bulk_migration_primary_action_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "🔍 Preview Green Migration (Dry Run)" in text
    assert "⚡ Approve All Green Tables" in text
    assert "No tables are currently ready for bulk approval." in text


def test_ask_agent_prefill_per_tab_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "💬 Ask Agent" in text
    assert "Summarize the migration readiness of this report." in text
    assert "Which domain should I migrate first and why?" in text
    assert "Show me all tables ready for bulk migration." in text
    assert "Are there any unmapped columns I should resolve before migrating?" in text
    assert "Which tables have the most downstream dependencies?" in text
    assert "Help me decide on the right confidence threshold for bulk approval." in text
    assert "agent_prefill" in text
    assert "agent_tab_active" in text


def test_agent_chat_ui_location_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert 'user_prompt = st.chat_input(' in text
    tab_start = text.index("with analysis_tabs[0]:")
    exec_start = text.index("with execution_tabs[0]:")
    analysis_chunk = text[tab_start:exec_start]
    assert "st.chat_input(" not in analysis_chunk


def test_migrate_button_shows_approval_panel() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "▶ Migrate" in text
    assert "pending_write_" in text
    assert "⏳ Awaiting Approval —" in text
    assert "✅ Approve & Write" in text
    assert "❌ Reject" in text
    assert "_render_pending_write_panel(" in text


def test_pending_write_panel_runs_write_and_test_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "def _render_pending_write_panel(" in text
    assert "_write_model_files(" in text
    assert "migration_agent_tools.test_model(" in text
    assert "Approve Corrected SQL" in text


def test_bulk_approve_selected_executes_pipeline_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    bulk_text = Path("src/ama/bulk_runner.py").read_text(encoding="utf-8")
    assert "bulk_approve_selected" in text
    assert "run_selected = st.button(btn_label" in text
    assert "migration_agent_tools.propose_dbt_model(" in text
    assert "_write_model_files(" in text
    assert "migration_agent_tools.test_model(" in text
    assert "append_decision(" in bulk_text
    assert "type CONFIRM to proceed" in text


def test_bulk_progress_ui_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "Bulk job running:" in text
    assert "st.progress((completed / max(1, total))" in text
    assert "Bulk migration started in background." in text


def test_migrated_table_done_indicator_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    bulk_text = Path("src/ama/bulk_runner.py").read_text(encoding="utf-8")
    assert "\"migrated_tables\"" in text
    assert "rcol.success(\"✅ Done\")" in text
    assert "Hide migrated tables" in text
    assert "Migration finished:" in text
    assert "Auto-refreshing bulk job status..." in text
    assert "time.sleep(1.2)" in text
    assert "st.rerun()" in text
    assert "greens_remaining = [s for s in greens if s.table_key not in migrated_tables]" in text
    assert "yellows_remaining = [s for s in yellows if s.table_key not in migrated_tables]" in text
    assert "from ama.bulk_runner import (" in text
    assert "def _bulk_job_write(" in bulk_text
    assert "def _bulk_job_load(" in bulk_text
    assert "No active bulk job state found." in text
    assert "Bulk workers: prepare/write=" in text
    assert "workers prep=" in text
    assert "\"dbt_workers\":" in text
    assert "Batch dbt validation for prepared models to reduce process-start overhead on large bulks." in bulk_text
    assert "Keep parity with single-table execution: one auto-fix pass per failed model." in bulk_text
    assert "Show failed tables and reasons" in text
    assert "Bulk dbt Validation Workers" in text
    assert "test_models_batch(" in bulk_text
    assert "_bulk_job_id, _bulk_job_state, _bulk_applied_now = _apply_bulk_completion_once(" in text
    assert "if isinstance(_bulk_job_state, dict):" in text
    assert "if bool(_bulk_applied_now):" in text
    assert "if s.queue == \"green\" and str(s.table_key) not in migrated_tables" in text


def test_bulk_dialect_flows_to_proposals_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    assert "Target Dialect" in text
    assert "key=\"bulk_dialect\"" in text
    assert "st.session_state[\"migration_dialect\"]" in text
    assert "dialect=str(st.session_state.get(\"migration_dialect\") or \"duckdb\")" in text


def test_score_confidence_without_glossary_uses_alias_merge_label() -> None:
    report = {
        "glossary_source": {"total_entries": 0, "layers": [], "glossary_paths_resolved": []},
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "dbo.customers",
                    "canonical_column": "customer_id",
                    "merge_confidence": 0.98,
                }
            ]
        },
    }
    result = score_confidence(
        inventory_row={"full_name": "dbo.customers"},
        report=report,
        column_defs=[{"name": "customer_id", "type": "int"}],
    )
    assert "alias merge matches" in result.reason
    assert "glossary" not in result.reason.lower().split("alias merge")[0]
    assert "merge_match" in result.components


def test_bulk_parallel_workers_control_and_executor_source_text() -> None:
    text = Path("src/ama/ui/dashboard.py").read_text(encoding="utf-8")
    bulk_text = Path("src/ama/bulk_runner.py").read_text(encoding="utf-8")
    assert "Bulk Parallel Workers" in text
    assert "ThreadPoolExecutor(max_workers=workers)" in bulk_text
    assert "as_completed(" in bulk_text
