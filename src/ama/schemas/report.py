"""
Report and JSONL boundary models. Validation is best-effort at export time;
ingestion never aborts on schema drift.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

AMA_REPORT_SCHEMA_VERSION = "1.2"


class IngestionStats(BaseModel):
    """SQL log parse telemetry embedded in AMA JSON reports (schema 1.1+)."""

    model_config = ConfigDict(extra="allow")

    total_rows: int = 0
    parse_ok: int = 0
    regex_fallback: int = 0
    skipped_empty: int = 0


class ReportModel(BaseModel):
    """
    Required contract fields for CLI JSON export. Extra top-level keys (columns, discovery, …)
    are preserved for full-report validation.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = AMA_REPORT_SCHEMA_VERSION
    migration_context: str = ""
    generated_at: str = Field(..., description="ISO 8601 timestamp (UTC recommended)")
    ingestion_stats: IngestionStats
    inventory: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional inventory snapshot; often under discovery.inventory",
    )
    alias_merge: dict[str, Any] | None = None
    scale_engine: dict[str, Any] | None = None


def validate_report_model(report: dict[str, Any]) -> None:
    """Validate export dict against ReportModel; raises ValidationError on failure."""
    ReportModel.model_validate(report)


class SqlLogRecord(BaseModel):
    """Loose JSONL row shape for optional ingest validation."""

    model_config = ConfigDict(extra="ignore")

    sql: str | None = None
    query: str | None = None
    statement: str | None = None
    env: str | None = None
    dialect: str | None = None


class MergedEntitySchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_column: str = ""
    source_columns: list[str] = Field(default_factory=list)
    merge_confidence: float = 0.0
    strategies: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    source_table: str = ""


class ReviewCandidateSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    legacy_name: str = ""
    suggested_ddl: str = ""
    merge_confidence: float = 0.0
    citation: str = ""
    strategy: str = ""
    source_table: str = ""
    category: str | None = None


class AliasMergeBlockSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    merged_entities: list[dict[str, Any]] = Field(default_factory=list)
    review_candidates: list[dict[str, Any]] = Field(default_factory=list)
    trash_candidates: list[dict[str, Any]] = Field(default_factory=list)


class DiscoveryInventoryRowSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = ""
    business_domain: str | None = None
    query_count: int = 0


class PlannedTableMigrationSchema(BaseModel):
    """Planner / migration wave table row (subset; extra fields allowed on export)."""

    model_config = ConfigDict(extra="ignore")

    full_name: str = ""
    is_broken: bool = False
    missing_parents: list[str] = Field(default_factory=list)
    reason: str = ""


class AmaReportBoundarySchema(BaseModel):
    """Top-level keys we expect in a normal AMA export (all optional slices)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str | None = None
    migration_context: str = ""
    target_table: str = ""
    queries_matched: int = 0
    discovery: dict[str, Any] = Field(default_factory=dict)
    alias_merge: dict[str, Any] | None = None
    importance_ddl: list[dict[str, Any]] = Field(default_factory=list)
    scale_engine: dict[str, Any] | None = None


def validate_report_boundary(report: dict[str, Any]) -> tuple[int, list[str]]:
    """
    Validate report dict against boundary models. Returns (error_count, sample_messages).
    Never raises — used to populate ingestion_stats / CI diagnostics.
    """
    errors: list[str] = []
    try:
        AmaReportBoundarySchema.model_validate(report)
    except Exception as e:  # noqa: BLE001 — aggregate validation
        errors.append(str(e)[:500])

    am = report.get("alias_merge")
    if isinstance(am, dict):
        for key in ("merged_entities", "review_candidates", "trash_candidates"):
            rows = am.get(key) or []
            if not isinstance(rows, list):
                errors.append(f"alias_merge.{key} is not a list")
                continue
            for i, row in enumerate(rows[:50]):
                if not isinstance(row, dict):
                    errors.append(f"alias_merge.{key}[{i}] not an object")
                    continue
                try:
                    if key == "merged_entities":
                        MergedEntitySchema.model_validate(row)
                    elif key in ("review_candidates", "trash_candidates"):
                        ReviewCandidateSchema.model_validate(row)
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{key}[{i}]: {ex!s}"[:300])
                    if len(errors) >= 20:
                        break

    disc = report.get("discovery")
    if isinstance(disc, dict):
        inv = disc.get("inventory")
        if inv is not None and not isinstance(inv, list):
            errors.append("discovery.inventory is not a list")
        elif isinstance(inv, list):
            for i, row in enumerate(inv[:100]):
                if isinstance(row, dict):
                    try:
                        DiscoveryInventoryRowSchema.model_validate(row)
                    except Exception as ex:  # noqa: BLE001
                        errors.append(f"inventory[{i}]: {ex!s}"[:300])
                        if len(errors) >= 25:
                            break

    return len(errors), errors[:15]
