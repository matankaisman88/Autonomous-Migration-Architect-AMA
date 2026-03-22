from __future__ import annotations

from pathlib import Path

import pytest

from ama.alias_resolver import AliasResolver, HITL_THRESHOLD, load_glossary
from ama.sanitize import is_generic_low_signal_name, sanitize_text
from ama.sql_pipeline import ColumnStats, TableColumnStats, process_sql_log_file, run_sql_logs_pipeline

ROOT = Path(__file__).resolve().parents[1]
DDL = ROOT / "sample_data" / "ddl" / "orders_columns.json"
GLOSS = ROOT / "sample_data" / "glossary" / "he_en_columns.json"


def test_sanitizer_strips_null_and_controls() -> None:
    raw = "SELECT\x00\x01\x02 a FROM t"
    s = sanitize_text(raw)
    assert "\x00" not in s
    assert "\x01" not in s


def test_generic_names_flagged() -> None:
    assert is_generic_low_signal_name("flag_1")
    assert is_generic_low_signal_name("temp_001")
    assert not is_generic_low_signal_name("customer_id")


def test_hebrew_status_merges_with_ddl_status() -> None:
    ddl = ["order_id", "customer_id", "status", "amount", "created_at"]
    gloss = load_glossary(GLOSS)
    r = AliasResolver(ddl_columns=ddl, glossary=gloss)
    hebrew_status = "\u05e1\u05d8\u05d8\u05d5\u05e1"  # סטטוס
    mc = r.propose_merge(hebrew_status)
    assert mc.ddl_column == "status"
    assert mc.merge_confidence >= 0.8
    assert not mc.hitl
    assert "Glossary" in mc.citation

    stats = TableColumnStats()
    stats.columns[hebrew_status] = ColumnStats(select=5)
    stats.columns["status"] = ColumnStats(select=3)
    mr = r.merge_table_stats(stats)
    merged = mr.merged_stats
    entities = mr.confirmed_entities
    assert merged.columns["status"].select == 8
    ent = next(e for e in entities if e.canonical_column == "status")
    assert len(ent.source_columns) == 2
    assert ent.merge_confidence >= 0.8


def test_tier3_trash_sql_logs_no_crash(tmp_path: Path) -> None:
    p = ROOT / "sample_data" / "stress_tier3" / "sql_logs" / "trash.jsonl"
    if not p.exists():
        pytest.skip("stress fixtures missing; run tools/generate_stress_samples.py")
    stats = process_sql_log_file(p, target_full_table="sales.orders", env="prod")
    assert stats.query_count >= 0


def test_tier3_low_confidence_generic_columns() -> None:
    ddl = ["order_id", "customer_id", "status", "amount", "created_at"]
    r = AliasResolver(ddl_columns=ddl, glossary=load_glossary(GLOSS))
    mc = r.propose_merge("flag_1")
    assert mc.hitl or mc.merge_confidence < HITL_THRESHOLD
    assert mc.merge_confidence < 0.8


def test_stress_tier3_pipeline_end_to_end() -> None:
    log = ROOT / "sample_data" / "stress_tier3" / "sql_logs" / "trash.jsonl"
    if not log.exists():
        pytest.skip("stress fixtures missing")
    stats = run_sql_logs_pipeline([log], target_full_table="sales.orders", env="prod")
    gloss = load_glossary(GLOSS)
    r = AliasResolver(ddl_columns=["order_id", "customer_id", "status", "amount", "created_at"], glossary=gloss)
    mr = r.merge_table_stats(stats)
    merged = mr.merged_stats
    props = mr.proposals
    assert merged.query_count == stats.query_count
    assert isinstance(props, list)
