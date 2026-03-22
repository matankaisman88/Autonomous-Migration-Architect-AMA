from __future__ import annotations

from pathlib import Path

from ama.alias_resolver import AliasResolver, load_glossary
from ama.sql_pipeline import ColumnStats, TableColumnStats

ROOT = Path(__file__).resolve().parents[1]
GLOSS = ROOT / "sample_data" / "glossary" / "he_en_columns.json"


def test_low_confidence_generics_not_merged_into_ddl_columns() -> None:
    """flag_1 / temp_001 must not be forced onto created_at (or any DDL) when confidence is trash."""
    ddl = ["order_id", "customer_id", "status", "amount", "created_at"]
    gloss = load_glossary(GLOSS)
    r = AliasResolver(
        ddl_columns=ddl,
        glossary=gloss,
        merge_floor=0.4,
        confirmed_threshold=0.8,
    )

    stats = TableColumnStats(query_count=100)
    stats.columns["flag_1"] = ColumnStats(select=50)
    stats.columns["temp_001"] = ColumnStats(select=40)
    stats.columns["created_at"] = ColumnStats(select=3)

    mr = r.merge_table_stats(stats)

    assert mr.merged_stats.columns["created_at"].select == 3
    assert "flag_1" in mr.unmapped_stats.columns
    assert "temp_001" in mr.unmapped_stats.columns
    trash_names = {u.legacy_name for u in mr.trash_candidates}
    assert "flag_1" in trash_names
    assert "temp_001" in trash_names
    for u in mr.trash_candidates:
        if u.legacy_name in ("flag_1", "temp_001"):
            assert u.merge_confidence < 0.4
