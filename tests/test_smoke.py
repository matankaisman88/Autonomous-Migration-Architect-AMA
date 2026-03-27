from __future__ import annotations

import os
from pathlib import Path

from ama.comms_ingest import aggregate_comms_for_table, mention_score
from ama.git_ingest import scan_git_sql_roots
from ama.importance import compute_importance_v0
from ama.sql_pipeline import parse_sql_query, run_sql_logs_pipeline


ROOT = Path(__file__).resolve().parents[1]


def test_parse_sql_extracts_columns() -> None:
    sql = "SELECT a, b FROM sales.orders WHERE status = 1"
    chunks, ok = parse_sql_query(sql, dialect="postgres")
    assert ok
    assert any("orders" in k or "sales.orders" in k for flat in chunks for k in flat)


def test_parse_sql_accepts_sqlserver_alias() -> None:
    sql = "SELECT o.id FROM sales.orders o"
    chunks, ok = parse_sql_query(sql, dialect="sqlserver")
    assert ok
    assert any("orders" in k or "sales.orders" in k for flat in chunks for k in flat)


def test_parse_sql_regex_mode_env_toggle() -> None:
    sql = "SELECT o.id FROM sales.orders o"
    os.environ["AMA_SQL_PARSE_MODE"] = "regex"
    try:
        chunks, ok = parse_sql_query(sql, dialect="sqlserver")
    finally:
        os.environ.pop("AMA_SQL_PARSE_MODE", None)
    assert chunks
    assert ok is False


def test_sql_logs_sample() -> None:
    p = ROOT / "sample_data" / "sql_logs" / "sample_file.jsonl"
    stats = run_sql_logs_pipeline([p], target_full_table="sales.orders", env="prod")
    assert stats.query_count >= 1
    assert "amount" in stats.columns or "order_id" in stats.columns


def test_comms_git_and_importance() -> None:
    comms = ROOT / "sample_data" / "comms"
    s, hits = aggregate_comms_for_table(comms, schema="sales", table="orders")
    assert mention_score("sales.orders revenue", "orders", "sales") > 0
    assert hits >= 1

    git_root = ROOT / "sample_data" / "git_repo" / "sql"
    gtot, gh = scan_git_sql_roots([git_root], schema="sales", table="orders")
    assert gtot > 0

    stats = run_sql_logs_pipeline(
        [ROOT / "sample_data" / "sql_logs" / "sample_file.jsonl"],
        target_full_table="sales.orders",
        env="prod",
    )
    rows = compute_importance_v0(
        stats,
        comms_score=s,
        comms_chunks=hits,
        git_score=gtot,
        git_hits=gh,
    )
    assert rows[0].importance_score >= rows[-1].importance_score
