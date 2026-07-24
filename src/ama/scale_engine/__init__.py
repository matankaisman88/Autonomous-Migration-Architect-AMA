from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ama.ddl_manifest import normalize_manifest_table_key
from ama.scale_engine.anomaly import AnomalyFlag, detect_anomalies, hitl_rejection_flags
from ama.scale_engine.contract import MigrationContract, build_contract
from ama.scale_engine.criticality import CriticalityResult, score_criticality
from ama.scale_engine.scorer import ConfidenceResult, score_confidence
from ama.schemas.report import manifest_scope_block_enabled, prepare_report_for_scoring

# Bulk gate defaults — must match POST /scale/{id}/evaluate (EvaluateRequest).
DEFAULT_CONF_FLOOR = 70
DEFAULT_CRIT_CEIL = 40


@dataclass
class ScoredTable:
    table_key: str
    queue: str
    confidence: int
    criticality: int
    anomaly_flags: list[AnomalyFlag]
    business_domain: str
    confidence_result: ConfidenceResult
    criticality_result: CriticalityResult


@dataclass
class BatchEvaluationResult:
    """Rollup from one :func:`evaluate_batch` pass over inventory.

    PRD-required fields: ``would_migrate``, ``would_flag_review``, ``would_block``,
    ``blocked_reasons``, ``contract_preview``, ``threshold_used``.
    ``scored_tables`` is an intentional extension beyond the PRD minimum: it holds
    per-table scores for the dashboard and agent tools without a second batch call.
    """

    would_migrate: int
    would_flag_review: int
    would_block: int
    blocked_reasons: list[dict[str, str]]
    contract_preview: MigrationContract
    threshold_used: dict[str, int]
    scored_tables: list[ScoredTable]


def _load_manifest_columns(report: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    alias_merge = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    manifest_raw = alias_merge.get("ddl_manifest")
    if not manifest_raw:
        return out
    manifest_path = Path(str(manifest_raw)).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        return out
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(manifest, dict):
        return out
    for table_key, ddl_path_raw in manifest.items():
        if str(table_key).startswith("_"):
            continue
        ddl_path = Path(str(ddl_path_raw)).expanduser()
        if not ddl_path.is_absolute():
            ddl_path = (manifest_path.parent / ddl_path).resolve()
        if not ddl_path.is_file():
            continue
        try:
            ddl_payload = json.loads(ddl_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        defs: list[dict[str, str]] = []
        if isinstance(ddl_payload, dict):
            cols = ddl_payload.get("columns")
            if isinstance(cols, list):
                for c in cols:
                    if isinstance(c, dict):
                        defs.append({"name": str(c.get("name") or ""), "type": str(c.get("type") or "")})
                    elif isinstance(c, str):
                        defs.append({"name": c, "type": _infer_type_from_name(c)})
        out[str(table_key)] = defs
    return out


def _infer_type_from_name(name: str) -> str:
    n = str(name or "").lower()
    if any(t in n for t in ("date", "time")):
        return "datetime2"
    if n.endswith("_id") or n == "id":
        return "int"
    if any(t in n for t in ("amount", "total", "price", "tax", "rate")):
        return "money"
    return "nvarchar"


def _column_defs_for_table(
    table_key: str, report: dict[str, Any], manifest_cols: dict[str, list[dict[str, str]]]
) -> list[dict[str, str]]:
    if table_key in manifest_cols and manifest_cols[table_key]:
        return manifest_cols[table_key]
    out: list[dict[str, str]] = []
    importance = report.get("importance_ddl")
    if isinstance(importance, list):
        for row in importance:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_table") or "") != table_key:
                continue
            col = str(row.get("column") or "")
            if col:
                dtype = str(row.get("data_type") or row.get("type") or "").strip() or _infer_type_from_name(col)
                out.append({"name": col, "type": dtype.lower()})
    return out


def _queue_for(conf: int, crit: int, flags: list[AnomalyFlag], conf_floor: int, crit_ceil: int) -> str:
    has_block = any(f.level == "BLOCK" for f in flags)
    has_warn = any(f.level == "WARN" for f in flags)
    if crit >= 80 or has_block or conf < 70:
        return "red"
    if 70 <= conf < conf_floor or has_warn:
        return "yellow"
    if conf >= conf_floor and crit <= crit_ceil and not has_block and not has_warn:
        return "green"
    return "yellow"


def evaluate_batch(
    report: dict[str, Any],  # PRD specifies ``dict``; ``dict[str, Any]`` is a stricter equivalent.
    dry_run: bool = False,
    conf_floor: int = DEFAULT_CONF_FLOOR,
    crit_ceil: int = DEFAULT_CRIT_CEIL,
) -> BatchEvaluationResult:
    _ = dry_run  # scoring is always non-mutating; kept for API compatibility
    prepare_report_for_scoring(report, strict=False)
    inv = (report.get("discovery") or {}).get("inventory") if isinstance(report.get("discovery"), dict) else []
    rows = [r for r in inv if isinstance(r, dict)] if isinstance(inv, list) else []
    manifest_cols = _load_manifest_columns(report)
    manifest_table_keys = {
        normalize_manifest_table_key(str(k))
        for k in (report.get("ddl_manifest_table_keys") or [])
        if str(k).strip()
    }
    manifest_table_keys.discard("")
    apply_manifest_scope, scope_skip_reason = manifest_scope_block_enabled(report)
    if scope_skip_reason:
        stats = report.get("ingestion_stats")
        if isinstance(stats, dict):
            existing = stats.get("report_normalization_warnings")
            if isinstance(existing, list):
                if scope_skip_reason not in existing:
                    stats["report_normalization_warnings"] = existing + [scope_skip_reason]
            else:
                stats["report_normalization_warnings"] = [scope_skip_reason]

    cluster_rows: dict[str, list[dict[str, Any]]] = {}
    cluster_types: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        table_key = str(row.get("full_name") or "")
        domain = str(row.get("business_domain") or "Unknown")
        defs = _column_defs_for_table(table_key, report, manifest_cols)
        cluster_rows.setdefault(domain, []).append(row)
        cluster_types.setdefault(domain, {})[table_key] = {
            d["name"]: d["type"] for d in defs if d.get("name")
        }

    scored: list[ScoredTable] = []
    blocked_reasons: list[dict[str, str]] = []
    for row in rows:
        table_key = str(row.get("full_name") or "")
        table_key_norm = normalize_manifest_table_key(table_key)
        domain = str(row.get("business_domain") or "Unknown")
        defs = _column_defs_for_table(table_key, report, manifest_cols)
        conf = score_confidence(inventory_row=row, report=report, column_defs=defs)
        crit = score_criticality(inventory_row=row, report=report)
        flags = detect_anomalies(
            inventory_row=row,
            report=report,
            cluster_rows=cluster_rows.get(domain, []),
            cluster_column_types=cluster_types.get(domain, {}),
            column_defs=defs,
        )
        flags.extend(hitl_rejection_flags(report, table_key))
        # Discovery can include non-manifest technical/legacy tables; keep bulk focused on migration scope.
        if apply_manifest_scope and manifest_table_keys and table_key_norm not in manifest_table_keys:
            flags.append(
                AnomalyFlag(
                    level="BLOCK",
                    name="outside_manifest_scope",
                    reason=f"{table_key} is not in ddl_manifest_table_keys",
                )
            )
        queue = _queue_for(conf.score, crit.score, flags, conf_floor, crit_ceil)
        if queue == "red":
            reason = next((f.reason for f in flags if f.level == "BLOCK"), "")
            blocked_reasons.append({"table_key": table_key, "reason": reason or conf.reason})
        scored.append(
            ScoredTable(
                table_key=table_key,
                queue=queue,
                confidence=conf.score,
                criticality=crit.score,
                anomaly_flags=flags,
                business_domain=domain,
                confidence_result=conf,
                criticality_result=crit,
            )
        )

    green_rows = [
        {
            "table_key": s.table_key,
            "confidence_components": s.confidence_result.components,
            "criticality_components": s.criticality_result.components,
        }
        for s in scored
        if s.queue == "green"
    ]
    all_rows = [{"table_key": s.table_key} for s in scored]
    contract = build_contract(green_rows=green_rows, all_rows=all_rows)
    return BatchEvaluationResult(
        would_migrate=sum(1 for s in scored if s.queue == "green"),
        would_flag_review=sum(1 for s in scored if s.queue == "yellow"),
        would_block=sum(1 for s in scored if s.queue == "red"),
        blocked_reasons=blocked_reasons,
        contract_preview=contract,
        threshold_used={"conf_floor": int(conf_floor), "crit_ceil": int(crit_ceil)},
        scored_tables=scored,
    )


def queue_emoji(queue: str) -> str:
    if queue == "green":
        return "🟢 Bulk"
    if queue == "yellow":
        return "🟡 Review"
    return "🔴 Blocked"
