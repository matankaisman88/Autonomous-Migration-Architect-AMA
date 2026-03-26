from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ama.scale_engine import evaluate_batch
from ama.ui.report_helpers import load_report_json


def _run_generator(out_dir: Path) -> Path:
    cmd = [
        sys.executable,
        "tools/generate_scale_engine_chaos.py",
        "--out",
        str(out_dir),
        "--seed",
        "42",
        "--tables",
        "100",
    ]
    subprocess.run(cmd, check=True)
    return out_dir / "chaos_report.json"


def _eval_by_table(report: dict) -> dict[str, object]:
    result = evaluate_batch(report=report, dry_run=True)
    return {s.table_key: s for s in result.scored_tables}


def test_named_green_table_is_green(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    assert by["logistics.delivery_status"].queue == "green"


def test_criticality_100_table_is_red(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    assert by["finance.core_ledger"].queue == "red"
    assert by["finance.core_ledger"].criticality == 100


def test_mixed_signal_criticality_wins(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    assert by["finance.payment_staging"].queue == "red"
    assert by["finance.payment_staging"].criticality >= 80


def test_blob_triggers_block(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    flags = by["finance.invoice_attachments"].anomaly_flags
    assert any(f.level == "BLOCK" for f in flags)
    assert by["finance.invoice_attachments"].queue == "red"


def test_ntext_triggers_block(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    flags = by["legacy.document_archive"].anomaly_flags
    assert any(f.level == "BLOCK" for f in flags)


def test_type_inconsistency_blocks_both_tables(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    sales_flags = by["sales.orders"].anomaly_flags
    crm_flags = by["crm.orders"].anomaly_flags
    assert any(f.level == "BLOCK" and "inconsistency" in f.name for f in sales_flags)
    assert any(f.level == "BLOCK" and "inconsistency" in f.name for f in crm_flags)


def test_column_count_outlier_warn(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    flags = by["finance.mega_journal"].anomaly_flags
    assert any(f.level == "WARN" for f in flags)
    assert any("column" in f.name and "outlier" in f.name for f in flags)


def test_zero_confidence_table_is_red(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    row = by["technical_debt.tbl_junk_7"]
    assert row.confidence == 0
    assert row.queue == "red"


def test_unclassified_cluster_no_crash(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    inv = report["discovery"]["inventory"]
    only = [r for r in inv if str(r.get("full_name", "")).startswith("unclassified.")]
    tiny = dict(report)
    tiny["discovery"] = {"inventory": only}
    out = evaluate_batch(report=tiny, dry_run=True)
    assert len(out.scored_tables) == 3
    assert all(s.queue in {"green", "yellow", "red"} for s in out.scored_tables)


def test_null_rate_warn_when_sample_present(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    by = _eval_by_table(report)
    flags = by["operations.import_staging"].anomaly_flags
    assert any(f.level == "WARN" and f.name == "high_null_rate" for f in flags)


def test_null_rate_skipped_when_sample_absent(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    for row in report["discovery"]["inventory"]:
        if row.get("full_name") == "operations.import_staging":
            row.pop("sample_rows", None)
            break
    by = _eval_by_table(report)
    flags = by["operations.import_staging"].anomaly_flags
    assert any(f.level == "INFO" and f.name == "null_rate_check_skipped" for f in flags)
    assert not any(f.level == "WARN" and f.name == "high_null_rate" for f in flags)


def test_queue_distribution_bands(tmp_path: Path) -> None:
    report = load_report_json(_run_generator(tmp_path))
    out = evaluate_batch(report=report, dry_run=True)
    green = sum(1 for s in out.scored_tables if s.queue == "green")
    yellow = sum(1 for s in out.scored_tables if s.queue == "yellow")
    red = sum(1 for s in out.scored_tables if s.queue == "red")
    assert green >= 20
    assert yellow >= 25
    assert red >= 20


def test_named_edge_case_table_count(tmp_path: Path) -> None:
    report_path = _run_generator(tmp_path)
    report = load_report_json(report_path)
    inv_names = {str(r.get("full_name")) for r in report["discovery"]["inventory"]}
    expected_11 = {
        "finance.core_ledger",
        "logistics.delivery_status",
        "finance.payment_staging",
        "finance.invoice_attachments",
        "legacy.document_archive",
        "sales.orders",
        "crm.orders",
        "finance.mega_journal",
        "technical_debt.tbl_junk_7",
        "operations.import_staging",
        "crm.CustomerProfiles",
    }
    assert expected_11.issubset(inv_names)
    assert len(report["discovery"]["inventory"]) == 100
    # File-level sanity that report is valid JSON and deterministic fields exist.
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["generated_at"] == "2026-01-01T00:00:00Z"
