from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConfidenceResult:
    score: int
    reason: str
    components: dict[str, int]


_TYPE_PATTERNS: dict[str, str] = {
    "datetime2": "timestamp",
    "datetime": "timestamp",
    "nvarchar": "varchar",
    "nchar": "char",
    "float": "double",
    "bit": "boolean",
    "money": "numeric",
}


def score_confidence(
    *,
    inventory_row: dict[str, Any],
    report: dict[str, Any],
    column_defs: list[dict[str, str]],
) -> ConfidenceResult:
    alias_merge = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    glossary_keys: set[str] = set()
    glossary_vals: set[str] = set()
    for k, v in alias_merge.items():
        if k in ("merged_entities", "review_candidates", "trash_candidates", "ddl_manifest"):
            continue
        if isinstance(k, str):
            glossary_keys.add(k.strip().lower())
        if isinstance(v, str):
            glossary_vals.add(v.strip().lower())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    glossary_vals.add(item.strip().lower())

    merge_by_col = _merge_confidence_by_column(report, str(inventory_row.get("full_name") or ""))

    total_columns = max(1, len(column_defs))
    matched_columns = 0
    pattern_hits = 0
    for col in column_defs:
        name = str(col.get("name") or "").strip().lower()
        ctype = str(col.get("type") or "").strip().lower()
        if not name:
            continue
        col_matched = (
            name in glossary_keys
            or name in glossary_vals
            or any(name in g for g in glossary_keys)
            or merge_by_col.get(name, 0.0) >= 0.85
        )
        if col_matched:
            matched_columns += 1
        if ctype and any(pat in ctype for pat in _TYPE_PATTERNS):
            pattern_hits += 1

    glossary_points = int(round((matched_columns / total_columns) * 70))
    type_points = int(round((pattern_hits / total_columns) * 30))
    score = max(0, min(100, glossary_points + type_points))
    reason = (
        f"glossary/merge matches {matched_columns}/{total_columns} columns, "
        f"type patterns {pattern_hits}/{total_columns}"
    )
    return ConfidenceResult(
        score=score,
        reason=reason,
        components={"glossary_match": glossary_points, "type_pattern": type_points},
    )


def _merge_confidence_by_column(report: dict[str, Any], table_key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    am = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    for bucket in ("merged_entities", "review_candidates"):
        for row in am.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_table") or "").strip() != table_key:
                continue
            ddl = str(row.get("canonical_column") or row.get("suggested_ddl") or "").strip().lower()
            if not ddl:
                continue
            try:
                mc = float(row.get("merge_confidence") or 0.0)
            except (TypeError, ValueError):
                mc = 0.0
            out[ddl] = max(out.get(ddl, 0.0), mc)
    return out
