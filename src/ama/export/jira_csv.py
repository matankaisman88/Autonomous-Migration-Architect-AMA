"""
Jira CSV import format: one row per discovery inventory table (UTF-8 with BOM).

Optimized for Jira Cloud CSV import: flat single-line Description, QUOTE_ALL,
no Project Key column (project chosen in UI). Hebrew-safe via utf-8-sig.

Used by ``ama-ingest export-plan --format jira`` and ``tools/report_to_jira_csv.py``.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

# No Project Key — assign project in Jira UI to avoid mapping errors.
CSV_FIELDNAMES = [
    "Summary",
    "Issue Type",
    "Priority",
    "Description",
    "Labels",
]


def flatten_for_jira_csv(text: str, *, sep: str = " | ") -> str:
    """
    Replace literal newlines and carriage returns so CSV rows stay single-line
    (Jira importer can hang on multi-line field values).
    """
    if not text:
        return ""
    s = str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in s.split("\n") if p.strip()]
    if not parts:
        return ""
    merged = sep.join(parts)
    # Catch any remaining control newlines
    merged = re.sub(r"[\n\r]+", " ", merged)
    merged = re.sub(r" +", " ", merged).strip()
    return merged


def label_single_tag(domain: str) -> str:
    """
    One Jira label token: no spaces, commas, or pipes (avoid multi-label / CSV breaks).
    Hebrew and letters preserved; whitespace → hyphen.
    """
    s = str(domain or "").strip()
    if not s:
        return "migration"
    s = re.sub(r"[\s,;|]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "migration"


def priority_from_score(score: Any) -> str:
    """Map AMA priority_score (0–100) to Jira priority names."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "Low"
    if s >= 80.0:
        return "Highest"
    if s >= 60.0:
        return "High"
    if s >= 40.0:
        return "Medium"
    return "Low"


def build_description(row: dict[str, Any]) -> str:
    """
    One flat string: business_description + technical_note (or status) + query_count.
    No newlines — safe for Jira CSV import.
    """
    bd = flatten_for_jira_csv(str(row.get("business_description") or ""))
    tn = str(row.get("technical_note") or "").strip()
    if not tn:
        tn = str(row.get("status") or "").strip()
    tn = flatten_for_jira_csv(tn)
    try:
        qc = int(row.get("query_count") or 0)
    except (TypeError, ValueError):
        qc = 0
    segments: list[str] = []
    if bd:
        segments.append(f"Business: {bd}")
    if tn:
        segments.append(f"Technical: {tn}")
    segments.append(f"Query count (logs): {qc}")
    merged = " | ".join(segments)
    return flatten_for_jira_csv(merged)


def load_inventory_rows_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``discovery.inventory`` rows from an AMA report dict."""
    disc = report.get("discovery") if isinstance(report.get("discovery"), dict) else {}
    inv = disc.get("inventory") if isinstance(disc, dict) else None
    if not isinstance(inv, list):
        return []
    return [r for r in inv if isinstance(r, dict)]


def load_table_rows_from_json_file(path: Path) -> list[dict[str, Any]]:
    """Load rows from a JSON file: AMA report object or a bare list of table dicts."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return load_inventory_rows_from_report(raw if isinstance(raw, dict) else {})


def rows_to_jira_records(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map inventory rows to Jira CSV column dicts."""
    out: list[dict[str, str]] = []
    for row in rows:
        fn = str(row.get("full_name") or "").strip()
        if not fn:
            continue
        domain = label_single_tag(str(row.get("business_domain") or "").strip())
        out.append(
            {
                "Summary": f"Migrate: {fn}",
                "Issue Type": "Task",
                "Priority": priority_from_score(row.get("priority_score")),
                "Description": build_description(row),
                "Labels": domain,
            },
        )
    return out


def write_jira_csv_records(path: Path, records: list[dict[str, str]]) -> None:
    """Write CSV: UTF-8 with BOM, every field quoted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDNAMES,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        w.writeheader()
        for rec in records:
            w.writerow(rec)


def write_jira_csv_from_report(report: dict[str, Any], out_path: Path) -> int:
    """
    Write Jira CSV from discovery inventory. Returns number of data rows written (excludes header).
    """
    rows = load_inventory_rows_from_report(report)
    records = rows_to_jira_records(rows)
    write_jira_csv_records(out_path, records)
    return len(records)
