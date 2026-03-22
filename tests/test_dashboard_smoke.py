from __future__ import annotations

from ama.business_logic import build_business_glossary_entries, group_glossary_entries
from ama.ui.report_helpers import (
    _merge_rows_for_filters,
    _pct_confirmed,
    filter_glossary_grouped,
)


def test_pct_confirmed() -> None:
    am = {
        "merged_entities": [{"merge_confidence": 0.9}],
        "review_candidates": [{"merge_confidence": 0.5}],
        "trash_candidates": [],
    }
    assert _pct_confirmed(am) == 50.0


def test_merge_filters_domain() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "prod_sales.orders",
                    "business_domain": "Finance",
                    "portfolio_section": "Core Business",
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "prod_sales.orders",
                    "canonical_column": "amount",
                    "merge_confidence": 0.95,
                    "source_columns": ["סכום"],
                }
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    m, _, _ = _merge_rows_for_filters(
        report, domains=["Finance"], portfolio="All", conf_min=0.0
    )
    assert len(m) == 1
    m2, _, _ = _merge_rows_for_filters(
        report, domains=["Logistics"], portfolio="All", conf_min=0.0
    )
    assert len(m2) == 0


def test_merge_filters_conf_min() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "sales.orders",
                    "business_domain": "Finance",
                    "portfolio_section": "Core Business",
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "sales.orders",
                    "canonical_column": "status",
                    "merge_confidence": 0.95,
                    "source_columns": ["סטטוס"],
                },
                {
                    "source_table": "sales.orders",
                    "canonical_column": "order_id",
                    "merge_confidence": 0.98,
                    "source_columns": ["order_id"],
                },
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    hi, _, _ = _merge_rows_for_filters(
        report, domains=None, portfolio="All", conf_min=0.97
    )
    assert len(hi) == 1
    assert hi[0]["canonical_column"] == "order_id"


def test_filter_glossary_respects_conf_min() -> None:
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "sales.orders",
                    "business_domain": "Finance",
                    "portfolio_section": "Core Business",
                }
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "sales.orders",
                    "canonical_column": "status",
                    "merge_confidence": 0.95,
                    "source_columns": ["סטטוס"],
                },
                {
                    "source_table": "sales.orders",
                    "canonical_column": "order_id",
                    "merge_confidence": 0.98,
                    "source_columns": ["order_id"],
                },
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    raw = build_business_glossary_entries(report)
    grouped = group_glossary_entries(raw)
    filtered = filter_glossary_grouped(
        grouped,
        report=report,
        domains=None,
        portfolio="All",
        conf_min=0.97,
    )
    terms = {str(x.get("target_ddl")) for x in filtered}
    assert "order_id" in terms
    assert "status" not in terms
