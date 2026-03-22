from __future__ import annotations

from ama.schemas.report import AMA_REPORT_SCHEMA_VERSION, validate_report_boundary


def test_validate_report_boundary_minimal() -> None:
    r = {
        "schema_version": AMA_REPORT_SCHEMA_VERSION,
        "target_table": "s.t",
        "queries_matched": 1,
        "discovery": {"enabled": False},
        "alias_merge": None,
        "importance_ddl": [],
    }
    n_err, samples = validate_report_boundary(r)
    assert n_err == 0
    assert samples == []
