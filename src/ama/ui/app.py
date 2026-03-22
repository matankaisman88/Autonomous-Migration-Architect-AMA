"""
Legacy entry point; the full UI lives in :mod:`ama.ui.dashboard`.

``streamlit run -m ama.ui.dashboard`` or ``ama-dashboard --report-path report.json``.
"""

from __future__ import annotations

from ama.ui.dashboard import main
from ama.ui.report_helpers import (
    _domain_for_table,
    _high_risk_tables,
    _inventory_df,
    _merge_rows_for_filters,
    _pct_confirmed,
    load_report_json,
)

__all__ = [
    "main",
    "load_report_json",
    "_inventory_df",
    "_domain_for_table",
    "_merge_rows_for_filters",
    "_high_risk_tables",
    "_pct_confirmed",
]

if __name__ == "__main__":
    main()
