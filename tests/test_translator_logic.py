from __future__ import annotations

from ama.business_logic import (
    build_business_glossary_entries,
    expand_concept_query,
    group_glossary_entries,
    review_row_signature,
    semantic_concept_search,
)


def test_expand_concept_query_money() -> None:
    needles = expand_concept_query("כסף")
    assert "amount" in needles or "סכום" in needles


def test_glossary_entries() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "prod_sales.orders",
                    "business_domain": "Finance",
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "prod_sales.orders",
                    "canonical_column": "amount",
                    "merge_confidence": 0.92,
                    "source_columns": ["סכום", "amt"],
                    "citations": ["glossary"],
                }
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    g = build_business_glossary_entries(report)
    assert len(g) == 1
    assert g[0]["target_ddl"] == "amount"
    assert "סכום" in g[0]["legacy_columns"]


def test_semantic_search_finds_amount() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "prod_sales.orders",
                    "business_domain": "Finance",
                    "business_description": "orders",
                    "query_count": 10,
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "prod_sales.orders",
                    "canonical_column": "amount",
                    "merge_confidence": 0.9,
                    "source_columns": ["סכום"],
                }
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    r = semantic_concept_search(report, "כסף")
    assert r["column_hits"]


def test_group_glossary_collapses_duplicate_tables() -> None:
    entries = [
        {
            "id": "1",
            "kind": "confirmed",
            "target_ddl": "status",
            "legacy_columns": "status",
            "business_term": "Status",
            "source_table": "prod_sales.orders",
            "confidence": 0.9,
        },
        {
            "id": "2",
            "kind": "confirmed",
            "target_ddl": "status",
            "legacy_columns": "status",
            "business_term": "Status",
            "source_table": "prod_sales.orders_as_o",
            "confidence": 0.99,
        },
    ]
    g = group_glossary_entries(entries)
    assert len(g) == 1
    assert len(g[0]["source_tables"]) == 2
    assert g[0]["confidence_display"] == 0.99


def test_review_signature_stable() -> None:
    a = review_row_signature({"source_table": "a", "legacy_name": "x", "suggested_ddl": "y"})
    b = review_row_signature({"source_table": "a", "legacy_name": "x", "suggested_ddl": "y"})
    assert a == b
