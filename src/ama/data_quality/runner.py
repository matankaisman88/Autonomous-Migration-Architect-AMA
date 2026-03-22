"""
DQ runner — composes schema boundary validation with report-specific integrity checks.
"""

from __future__ import annotations

from typing import Any

from ama.data_quality.checks import DQCheckResult, DQSeverity, DQSuiteResult
from ama.schemas.report import AMA_REPORT_SCHEMA_VERSION, validate_report_boundary


def run_dq_suite(report: dict[str, Any]) -> DQSuiteResult:
    """
    Run all DQ checks on an AMA report dict (typically loaded from JSON).

    - **boundary**: Pydantic boundary validation (counts errors).
    - **schema_version**: expect ``schema_version`` when present for 1.1+ contracts.
    - **ingestion_stats**: optional but recommended for operational reports.
    - **discovery inventory**: if ``discovery.enabled``, inventory must be a non-empty list.
    """
    out = DQSuiteResult()

    n_err, samples = validate_report_boundary(report)
    if n_err > 0:
        msg = f"{n_err} boundary validation issue(s). First samples: {samples[:3]!s}"
        out.checks.append(
            DQCheckResult(
                name="report_boundary",
                severity=DQSeverity.ERROR,
                message=msg[:2000],
            )
        )
    else:
        out.checks.append(
            DQCheckResult(
                name="report_boundary",
                severity=DQSeverity.OK,
                message="AmaReportBoundarySchema validation passed",
            )
        )

    sv = report.get("schema_version")
    if sv is None:
        out.checks.append(
            DQCheckResult(
                name="schema_version",
                severity=DQSeverity.WARN,
                message="Missing schema_version — prefer AMA export with schema_version set.",
            )
        )
    elif str(sv) != AMA_REPORT_SCHEMA_VERSION:
        out.checks.append(
            DQCheckResult(
                name="schema_version",
                severity=DQSeverity.WARN,
                message=f"schema_version is {sv!r}, current contract is {AMA_REPORT_SCHEMA_VERSION!r}.",
            )
        )
    else:
        out.checks.append(
            DQCheckResult(
                name="schema_version",
                severity=DQSeverity.OK,
                message=f"schema_version is {AMA_REPORT_SCHEMA_VERSION}.",
            )
        )

    ing = report.get("ingestion_stats")
    if not isinstance(ing, dict):
        out.checks.append(
            DQCheckResult(
                name="ingestion_stats",
                severity=DQSeverity.WARN,
                message="Missing ingestion_stats block — regenerate with current ama-ingest.",
            )
        )
    else:
        out.checks.append(
            DQCheckResult(
                name="ingestion_stats",
                severity=DQSeverity.OK,
                message="ingestion_stats present.",
            )
        )

    disc = report.get("discovery")
    if isinstance(disc, dict) and disc.get("enabled"):
        inv = disc.get("inventory")
        if not isinstance(inv, list) or len(inv) == 0:
            out.checks.append(
                DQCheckResult(
                    name="discovery_inventory",
                    severity=DQSeverity.WARN,
                    message="discovery.enabled but inventory is empty — check SQL logs / discovery-mode.",
                )
            )
        else:
            out.checks.append(
                DQCheckResult(
                    name="discovery_inventory",
                    severity=DQSeverity.OK,
                    message=f"discovery inventory rows: {len(inv)}.",
                )
            )

    return out
