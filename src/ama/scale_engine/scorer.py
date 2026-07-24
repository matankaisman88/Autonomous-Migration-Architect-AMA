from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ALIAS_MERGE_RESERVED = frozenset(
    {"merged_entities", "review_candidates", "trash_candidates", "ddl_manifest"}
)


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


def _glossary_terms(report: dict[str, Any]) -> tuple[set[str], bool]:
    """Collect glossary terms from glossary_source and legacy flat alias_merge pairs."""
    terms: set[str] = set()
    from_glossary_source = False

    gs = report.get("glossary_source") if isinstance(report.get("glossary_source"), dict) else {}
    if int(gs.get("total_entries") or 0) > 0:
        from_glossary_source = True
        for layer in gs.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            for entry in layer.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                for key in ("source_term", "target_column"):
                    term = str(entry.get(key) or "").strip().lower()
                    if term:
                        terms.add(term)

    am = report.get("alias_merge") if isinstance(report.get("alias_merge"), dict) else {}
    for key, value in am.items():
        if key in _ALIAS_MERGE_RESERVED:
            continue
        if isinstance(key, str):
            term = key.strip().lower()
            if term:
                terms.add(term)
        if isinstance(value, str):
            term = value.strip().lower()
            if term:
                terms.add(term)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    term = item.strip().lower()
                    if term:
                        terms.add(term)

    return terms, from_glossary_source


def _column_glossary_hit(name: str, glossary_terms: set[str]) -> bool:
    if not glossary_terms:
        return False
    if name in glossary_terms:
        return True
    return any(name in term for term in glossary_terms)


def score_confidence(
    *,
    inventory_row: dict[str, Any],
    report: dict[str, Any],
    column_defs: list[dict[str, str]],
) -> ConfidenceResult:
    glossary_terms, glossary_loaded = _glossary_terms(report)
    merge_by_col = _merge_confidence_by_column(report, str(inventory_row.get("full_name") or ""))

    total_columns = max(1, len(column_defs))
    matched_columns = 0
    glossary_hits = 0
    pattern_hits = 0
    for col in column_defs:
        name = str(col.get("name") or "").strip().lower()
        ctype = str(col.get("type") or "").strip().lower()
        if not name:
            continue
        merge_hit = merge_by_col.get(name, 0.0) >= 0.85
        glossary_hit = _column_glossary_hit(name, glossary_terms)
        if merge_hit or glossary_hit:
            matched_columns += 1
        if glossary_hit:
            glossary_hits += 1
        if ctype and any(pat in ctype for pat in _TYPE_PATTERNS):
            pattern_hits += 1

    match_points = int(round((matched_columns / total_columns) * 70))
    type_points = int(round((pattern_hits / total_columns) * 30))
    score = max(0, min(100, match_points + type_points))
    if glossary_loaded and glossary_hits:
        reason = (
            f"glossary {glossary_hits}/{total_columns}, "
            f"alias merge {matched_columns}/{total_columns} columns, "
            f"type patterns {pattern_hits}/{total_columns}"
        )
        components = {
            "glossary_match": match_points,
            "type_pattern": type_points,
        }
    elif glossary_hits and not glossary_loaded:
        reason = (
            f"glossary/merge matches {matched_columns}/{total_columns} columns, "
            f"type patterns {pattern_hits}/{total_columns}"
        )
        components = {
            "glossary_match": match_points,
            "type_pattern": type_points,
        }
    else:
        reason = (
            f"alias merge matches {matched_columns}/{total_columns} columns, "
            f"type patterns {pattern_hits}/{total_columns}"
        )
        components = {
            "merge_match": match_points,
            "type_pattern": type_points,
        }
    return ConfidenceResult(
        score=score,
        reason=reason,
        components=components,
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
            for source_col in row.get("source_columns") or []:
                sc = str(source_col or "").strip().lower()
                if sc:
                    out[sc] = max(out.get(sc, 0.0), mc)
    return out
