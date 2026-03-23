from __future__ import annotations

from ama.business_logic import build_business_glossary_entries, group_glossary_entries
import pandas as pd

from ama.ui.report_helpers import (
    _merge_rows_for_filters,
    _pct_confirmed,
    filter_glossary_grouped,
    filter_merge_buckets_by_inventory,
    inventory_allowed_tables,
    pct_confirmed_filtered,
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


def test_filter_merge_buckets_aligns_executive_with_inventory_scope() -> None:
    """Merge rows can pass domain/portfolio filters while excluded from filtered inv_view."""
    report = {
        "discovery": {
            "inventory": [
                {
                    "full_name": "hr.employees",
                    "business_domain": "HR",
                    "portfolio_section": "",
                },
                {
                    "full_name": "hr.departments",
                    "business_domain": "HR",
                    "portfolio_section": "Core",
                },
            ]
        },
        "alias_merge": {
            "merged_entities": [
                {
                    "source_table": "hr.employees",
                    "canonical_column": "x",
                    "merge_confidence": 0.99,
                },
                {
                    "source_table": "hr.departments",
                    "canonical_column": "y",
                    "merge_confidence": 0.99,
                },
            ],
            "review_candidates": [],
            "trash_candidates": [],
        },
    }
    m, r, t = _merge_rows_for_filters(
        report, domains=["HR"], portfolio="Core", conf_min=0.0
    )
    assert len(m) == 2
    inv_view = pd.DataFrame(report["discovery"]["inventory"])
    inv_view = inv_view[inv_view["business_domain"] == "HR"]
    inv_view = inv_view[inv_view["portfolio_section"] == "Core"]
    allowed = inventory_allowed_tables(inv_view)
    assert allowed == {"hr.departments"}
    mf, rf, tf = filter_merge_buckets_by_inventory(m, r, t, allowed)
    assert len(mf) == 1 and mf[0]["source_table"] == "hr.departments"
    assert pct_confirmed_filtered(m, r, t) == 100.0
    assert pct_confirmed_filtered(mf, rf, tf) == 100.0
