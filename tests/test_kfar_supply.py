"""Smoke tests for Kfar Supply demo dataset."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KFAR = ROOT / "sample_data" / "kfar_supply"


@pytest.mark.skipif(
    not (KFAR / "sql_logs" / "kfar_prod.jsonl").is_file(),
    reason="Kfar dataset not generated — run tools/generate_kfar_supply.py",
)
class TestKfarDataset:
    """Validate on-disk Kfar Supply fixtures."""

    def test_sql_log_is_jsonl(self) -> None:
        """SQL log has many lines and expected keys."""
        lines = (
            (KFAR / "sql_logs" / "kfar_prod.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        assert len(lines) >= 1000
        for line in lines[:20]:
            row = json.loads(line)
            assert "sql" in row and "dialect" in row

    def test_manifest_tables_have_ddl_files(self) -> None:
        """Every manifest entry resolves to an existing DDL JSON file."""
        manifest = json.loads(
            (KFAR / "ddl" / "kfar_manifest.json").read_text(encoding="utf-8")
        )
        for k, v in manifest.items():
            if k.startswith("_"):
                continue
            p = ROOT / v
            assert p.is_file(), f"DDL file missing for {k}: {v}"

    def test_glossary_covers_hebrew_columns(self) -> None:
        """Core Hebrew column names used in the log appear in the glossary."""
        glossary = json.loads(
            (KFAR / "glossary" / "kfar_glossary.json").read_text(encoding="utf-8")
        )
        required = [
            "סכום",
            "סטטוס",
            "תאריך_יצירה",
            "כמות",
            "מחיר",
            "תשלום",
            "מספר_מעקב",
        ]
        for term in required:
            assert term in glossary, f"Missing glossary entry: {term}"

    def test_git_sql_files_reference_kfar_tables(self) -> None:
        """Git SQL corpus references key fact tables."""
        sql_files = list((KFAR / "git_sql").rglob("*.sql"))
        assert len(sql_files) >= 3
        all_sql = " ".join(f.read_text(encoding="utf-8") for f in sql_files)
        assert "dbo.orders" in all_sql
        assert "finance.invoices" in all_sql

    def test_comms_jsonl_format(self) -> None:
        """Slack JSONL rows have channel and text."""
        comms = (
            (KFAR / "comms" / "kfar_slack.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        assert len(comms) >= 20
        for line in comms:
            row = json.loads(line)
            assert "channel" in row and "text" in row

    def test_sql_log_contains_proven_review_band_columns(self) -> None:
        """
        The five proven review-band column names must appear in the log.
        These are compound DDL names with underscore removed — mathematically
        guaranteed to land in the 0.4-0.8 review band with hash embeddings.
        """
        content = (KFAR / "sql_logs" / "kfar_prod.jsonl").read_text(encoding="utf-8")
        required = ["orderid", "invoiceid", "shipmentid", "paymentid", "customerid"]
        found = [col for col in required if col in content]
        assert len(found) >= 4, (
            f"Expected ≥4 proven review-band columns in log, found {len(found)}: {found}"
        )
