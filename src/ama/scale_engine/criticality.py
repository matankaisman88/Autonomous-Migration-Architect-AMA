from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class CriticalityResult:
    score: int
    reason: str
    components: dict[str, int]


_SENSITIVE_NAME_RE = re.compile(
    r"(amount|balance|tax|invoice|payment|salary|ssn|credit|revenue)",
    re.IGNORECASE,
)


def score_criticality(*, inventory_row: dict[str, Any], report: dict[str, Any]) -> CriticalityResult:
    full_name = str(inventory_row.get("full_name") or "").strip()
    edges = (report.get("lineage") or {}).get("edges") if isinstance(report.get("lineage"), dict) else []
    downstream = 0
    if isinstance(edges, list):
        for edge in edges:
            if isinstance(edge, dict) and str(edge.get("source") or "").strip() == full_name:
                downstream += 1
    if downstream <= 0:
        lineage_points = 0
    elif downstream == 1:
        lineage_points = 15
    elif downstream == 2:
        lineage_points = 25
    else:
        lineage_points = 40

    try:
        qcount = int(inventory_row.get("query_count") or 0)
    except (TypeError, ValueError):
        qcount = 0
    if qcount <= 10:
        usage_points = 0
    elif qcount <= 50:
        usage_points = 10
    elif qcount <= 200:
        usage_points = 20
    elif qcount <= 500:
        usage_points = 30
    else:
        usage_points = 35

    naming_hit = bool(_SENSITIVE_NAME_RE.search(full_name))
    if not naming_hit:
        table_cols = {
            str(row.get("column") or "").strip()
            for row in (report.get("importance_ddl") or [])
            if isinstance(row, dict) and str(row.get("source_table") or "").strip() == full_name
        }
        naming_hit = any(_SENSITIVE_NAME_RE.search(c) for c in table_cols)
    naming_points = 25 if naming_hit else 0
    score = max(0, min(100, lineage_points + usage_points + naming_points))
    reason = f"query_count={qcount}, {downstream} downstream deps, table={full_name}"
    return CriticalityResult(
        score=score,
        reason=reason,
        components={"lineage": lineage_points, "usage": usage_points, "naming": naming_points},
    )
