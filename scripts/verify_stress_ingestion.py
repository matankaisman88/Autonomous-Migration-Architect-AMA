#!/usr/bin/env python3
"""
Verification harness: Tier-3 trash logs must not crash ingestion; generic columns
should skew toward HITL / low merge_confidence.
Run from repo root: python scripts/verify_stress_ingestion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ama.alias_resolver import AliasResolver, HITL_THRESHOLD, load_ddl_columns, load_glossary
from ama.sql_pipeline import run_sql_logs_pipeline


def main() -> int:
    ddl_path = ROOT / "sample_data" / "ddl" / "orders_columns.json"
    gloss_path = ROOT / "sample_data" / "glossary" / "he_en_columns.json"
    trash = ROOT / "sample_data" / "stress_tier3" / "sql_logs" / "trash.jsonl"
    if not trash.exists():
        print("FAIL: run tools/generate_stress_samples.py first")
        return 2

    ddl = load_ddl_columns(ddl_path)
    gloss = load_glossary(gloss_path)
    stats = run_sql_logs_pipeline([trash], target_full_table="sales.orders", env="prod")
    resolver = AliasResolver(ddl_columns=ddl, glossary=gloss)
    mr = resolver.merge_table_stats(stats)
    merged = mr.merged_stats
    props = mr.proposals
    entities = mr.confirmed_entities

    low = [p for p in props if p.merge_confidence < HITL_THRESHOLD]
    print(f"queries_matched={stats.query_count} merged_entities={len(entities)} hitl_or_low_conf={len(low)}")
    for p in props:
        if "flag_1" in p.log_column or "temp_001" in p.log_column:
            assert p.hitl or p.merge_confidence < HITL_THRESHOLD, p

    print("verify_stress_ingestion: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
