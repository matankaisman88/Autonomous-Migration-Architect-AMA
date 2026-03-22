from __future__ import annotations

from ama.ui.report_helpers import _merge_rows_for_filters, _pct_confirmed


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
