"""
Report and JSONL boundary models. Validation is enforced at report load; CLI export
still records advisory counts in ingestion_stats for diagnostics.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ama.ddl_manifest import normalize_manifest_table_key
from ama.planner.broken_lineage import manifest_normalized_keys

logger = logging.getLogger(__name__)

AMA_REPORT_SCHEMA_VERSION = "1.2"

_ALIAS_MERGE_RESERVED = frozenset(
    {"merged_entities", "review_candidates", "trash_candidates", "ddl_manifest"}
)

# When >= this many inventory rows and <5% overlap with manifest keys, skip scope blocks.
_MANIFEST_SCOPE_MIN_BATCH = 10
_MANIFEST_SCOPE_MIN_OVERLAP_RATIO = 0.05


class ReportBoundaryError(ValueError):
    """Raised when a report fails boundary validation at load/evaluate time."""


class IngestionStats(BaseModel):
    """SQL log parse telemetry embedded in AMA JSON reports (schema 1.1+)."""

    model_config = ConfigDict(extra="allow")

    total_rows: int = 0
    parse_ok: int = 0
    regex_fallback: int = 0
    skipped_empty: int = 0


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

    merged_entities: list[MergedEntitySchema] = Field(default_factory=list)
    review_candidates: list[ReviewCandidateSchema] = Field(default_factory=list)
    trash_candidates: list[ReviewCandidateSchema] = Field(default_factory=list)
    ddl_manifest: str | None = None


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
    alias_merge: AliasMergeBlockSchema | None = None
    scale_engine: dict[str, Any] | None = None


class SqlLogRecord(BaseModel):
    """Loose JSONL row shape for optional ingest validation."""

    model_config = ConfigDict(extra="ignore")

    sql: str | None = None
    query: str | None = None
    statement: str | None = None
    env: str | None = None
    dialect: str | None = None


class AmaReportBoundarySchema(BaseModel):
    """Top-level keys we expect in a normal AMA export (all optional slices)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str | None = None
    migration_context: str = ""
    target_table: str = ""
    queries_matched: int = 0
    discovery: dict[str, Any] = Field(default_factory=dict)
    alias_merge: AliasMergeBlockSchema | None = None
    importance_ddl: list[dict[str, Any]] = Field(default_factory=list)
    scale_engine: dict[str, Any] | None = None


def validate_report_model(report: dict[str, Any]) -> None:
    """Validate export dict against ReportModel; raises ValidationError on failure."""
    ReportModel.model_validate(report)


def _ddl_manifest_table_keys_from_report(report: dict[str, Any]) -> list[str]:
    am = report.get("alias_merge")
    if not isinstance(am, dict):
        return []
    raw = am.get("ddl_manifest")
    if not raw:
        return []
    manifest_path = Path(str(raw)).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(manifest, dict):
        return []
    return sorted(manifest_normalized_keys(manifest))


def _promote_legacy_alias_merge_glossary(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Move flat glossary key→value pairs out of alias_merge into a structured block."""
    am = report.get("alias_merge")
    if not isinstance(am, dict):
        return {}, 0

    legacy_entries: list[dict[str, str]] = []
    structured: dict[str, Any] = {}
    for key, value in am.items():
        if key in _ALIAS_MERGE_RESERVED:
            structured[key] = value
            continue
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip() and not key.startswith("_"):
            legacy_entries.append({"source_term": key.strip(), "target_column": value.strip()})

    if legacy_entries:
        gs = report.get("glossary_source")
        if not isinstance(gs, dict) or int(gs.get("total_entries") or 0) <= 0:
            report["glossary_source"] = {
                "layers": [
                    {
                        "file": "alias_merge_legacy",
                        "path_relative": "",
                        "path_absolute": "",
                        "layer": "legacy",
                        "entry_count": len(legacy_entries),
                        "entries": legacy_entries,
                    }
                ],
                "total_entries": len(legacy_entries),
                "glossary_paths_resolved": [],
            }

    return structured, len(legacy_entries)


def normalize_report_contract(report: dict[str, Any]) -> list[str]:
    """
    Normalize legacy report shapes in-place before scoring or strict validation.

    - Promote flat glossary pairs embedded in ``alias_merge`` into ``glossary_source``.
    - Coerce ``alias_merge`` to the structured AliasMergeBlock shape.
    - Populate ``ddl_manifest_table_keys`` from ``alias_merge.ddl_manifest`` when absent.
    """
    warnings: list[str] = []

    structured_am, promoted = _promote_legacy_alias_merge_glossary(report)
    if promoted:
        warnings.append(
            f"Promoted {promoted} legacy glossary entries from alias_merge into glossary_source"
        )
    if structured_am or report.get("alias_merge") is not None:
        try:
            block = AliasMergeBlockSchema.model_validate(structured_am or {})
            report["alias_merge"] = block.model_dump(exclude_none=True)
        except ValidationError as exc:
            warnings.append(f"alias_merge normalization failed: {exc!s}"[:300])

    if not report.get("ddl_manifest_table_keys"):
        derived = _ddl_manifest_table_keys_from_report(report)
        if derived:
            report["ddl_manifest_table_keys"] = derived
            warnings.append(
                f"Derived ddl_manifest_table_keys ({len(derived)} tables) from alias_merge.ddl_manifest"
            )

    return warnings


def manifest_scope_block_enabled(report: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Return whether ``outside_manifest_scope`` blocks should apply for this report.

    Blocks apply only when manifest keys are non-empty and overlap discovery inventory
    enough to indicate the keys use the same naming convention (not a silent total mismatch).
    """
    raw_keys = report.get("ddl_manifest_table_keys") or []
    manifest_norm = {
        normalize_manifest_table_key(str(k))
        for k in raw_keys
        if str(k).strip()
    }
    manifest_norm.discard("")
    if not manifest_norm:
        return False, None

    inv = (report.get("discovery") or {}).get("inventory") if isinstance(report.get("discovery"), dict) else []
    rows = [r for r in inv if isinstance(r, dict)] if isinstance(inv, list) else []
    if not rows:
        return False, None

    in_scope = sum(
        1
        for row in rows
        if normalize_manifest_table_key(str(row.get("full_name") or "")) in manifest_norm
    )
    overlap_ratio = in_scope / len(rows)
    if len(rows) >= _MANIFEST_SCOPE_MIN_BATCH and overlap_ratio < _MANIFEST_SCOPE_MIN_OVERLAP_RATIO:
        msg = (
            f"ddl_manifest_table_keys overlaps only {in_scope}/{len(rows)} discovery tables "
            f"({overlap_ratio:.0%}) — skipping outside_manifest_scope blocks (likely naming mismatch)"
        )
        return False, msg
    return True, None


def _collect_boundary_errors(report: dict[str, Any]) -> list[str]:
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
                    else:
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

    return errors


def validate_report_boundary(
    report: dict[str, Any],
    *,
    strict: bool = False,
) -> tuple[int, list[str]]:
    """
    Validate report dict against boundary models.

    When ``strict`` is False (default), returns ``(error_count, sample_messages)`` for
    advisory / DQ use. When ``strict`` is True, raises :class:`ReportBoundaryError`.
    """
    errors = _collect_boundary_errors(report)
    samples = errors[:15]
    if strict and errors:
        raise ReportBoundaryError("; ".join(samples[:5]))
    return len(errors), samples


def prepare_report_for_scoring(report: dict[str, Any], *, strict: bool = True) -> list[str]:
    """
    Normalize legacy shapes, record warnings on the report, and optionally enforce boundary validation.
    """
    warnings = normalize_report_contract(report)
    if warnings:
        stats = report.get("ingestion_stats")
        if not isinstance(stats, dict):
            stats = {}
            report["ingestion_stats"] = stats
        existing = stats.get("report_normalization_warnings")
        if isinstance(existing, list):
            stats["report_normalization_warnings"] = existing + warnings
        else:
            stats["report_normalization_warnings"] = warnings
        for msg in warnings:
            logger.warning("report normalization: %s", msg)

    if strict:
        validate_report_boundary(report, strict=True)
    return warnings
