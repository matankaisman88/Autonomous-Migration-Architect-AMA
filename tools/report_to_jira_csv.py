#!/usr/bin/env python3
"""
Convert an AMA JSON report (or a list of table rows) into a Jira-friendly CSV import file.

This is a thin CLI around :mod:`ama.export.jira_csv` (same format as ``ama-ingest export-plan --format jira``).
No Project Key column — choose the project in Jira when importing.

**Shell note:** ``^`` continues lines only in **cmd.exe**. In **Git Bash / sh**, use one line, or end each
line with ``\\`` (backslash), not ``^``.

Examples::

  # One line — works in Git Bash, PowerShell, cmd
  python tools/report_to_jira_csv.py -i hr_report.json -o jira_import.csv

  # Git Bash / macOS / Linux (backslash continuation)
  python tools/report_to_jira_csv.py \\
    -i hr_report.json \\
    -o jira_import.csv

  # Windows cmd.exe only (caret at end of each line)
  python tools/report_to_jira_csv.py ^
    -i hr_report.json ^
    -o jira_import.csv

UTF-8 with BOM; all fields quoted; descriptions flattened for Jira importer stability.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ama.export.jira_csv import (
    load_table_rows_from_json_file,
    rows_to_jira_records,
    write_jira_csv_records,
)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Convert AMA report JSON to Jira CSV (discovery inventory rows).",
        epilog=(
            "In Git Bash, use one line or \\ continuation — not ^ (caret is for cmd.exe only)."
        ),
    )
    p.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to hr_report.json (or a JSON array of table rows).",
    )
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Output CSV path.",
    )
    args = p.parse_args()

    rows = load_table_rows_from_json_file(args.input)
    records = rows_to_jira_records(rows)
    write_jira_csv_records(args.output, records)
    print(f"Wrote {len(records)} row(s) to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
