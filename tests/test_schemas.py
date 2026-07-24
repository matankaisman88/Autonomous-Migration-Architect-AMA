from __future__ import annotations

import pytest

from ama.schemas.report import (
    AMA_REPORT_SCHEMA_VERSION,
    ReportBoundaryError,
    normalize_report_contract,
    prepare_report_for_scoring,
    validate_report_boundary,
)


def _minimal_boundary_report(**overrides: object) -> dict:
    report = {
        "schema_version": AMA_REPORT_SCHEMA_VERSION,
        "migration_context": "s.t",
        "queries_matched": 1,
        "discovery": {"enabled": False},
        "alias_merge": None,
        "importance_ddl": [],
    }
    report.update(overrides)
    return report


def test_validate_report_boundary_minimal() -> None:
    r = {
        "schema_version": AMA_REPORT_SCHEMA_VERSION,
        "migration_context": "s.t",
        "queries_matched": 1,
        "discovery": {"enabled": False},
        "alias_merge": None,
        "importance_ddl": [],
    }
    n_err, samples = validate_report_boundary(r)
    assert n_err == 0
    assert samples == []


def test_validate_report_boundary_strict_raises_on_malformed_alias_merge() -> None:
    report = _minimal_boundary_report(
        alias_merge={"merged_entities": ["not-a-dict"]},
    )
    with pytest.raises(ReportBoundaryError) as exc_info:
        validate_report_boundary(report, strict=True)
    detail = str(exc_info.value)
    assert "merged_entities" in detail or "not an object" in detail


def test_normalize_report_contract_promotes_legacy_alias_merge_glossary() -> None:
    report = _minimal_boundary_report(
        alias_merge={
            "customer_id": "customer_id",
            "segment": "segment",
        },
        ingestion_stats={},
    )
    warnings = normalize_report_contract(report)
    assert any("legacy glossary" in w for w in warnings)
    gs = report.get("glossary_source") or {}
    assert int(gs.get("total_entries") or 0) == 2
    am = report.get("alias_merge") or {}
    assert "customer_id" not in am
    assert "segment" not in am
    assert set(am.keys()) <= {"merged_entities", "review_candidates", "trash_candidates", "ddl_manifest"}


def test_prepare_report_for_scoring_records_legacy_promotion_warnings() -> None:
    report = _minimal_boundary_report(
        alias_merge={"legacy_col": "canonical_col"},
        ingestion_stats={},
    )
    prepare_report_for_scoring(report, strict=False)
    stats = report["ingestion_stats"]
    warnings = stats.get("report_normalization_warnings") or []
    assert any("legacy glossary" in w for w in warnings)
    assert int((report.get("glossary_source") or {}).get("total_entries") or 0) == 1
    assert "legacy_col" not in (report.get("alias_merge") or {})
