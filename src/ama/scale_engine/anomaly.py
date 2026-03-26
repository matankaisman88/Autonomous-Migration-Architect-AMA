from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any


@dataclass
class AnomalyFlag:
    level: str
    name: str
    reason: str


_BLOB_TYPES = {"blob", "clob", "image", "varbinary", "ntext", "text"}


def detect_anomalies(
    *,
    inventory_row: dict[str, Any],
    report: dict[str, Any],
    cluster_rows: list[dict[str, Any]],
    cluster_column_types: dict[str, dict[str, str]],
    column_defs: list[dict[str, str]],
) -> list[AnomalyFlag]:
    out: list[AnomalyFlag] = []
    table_key = str(inventory_row.get("full_name") or "")
    domain = str(inventory_row.get("business_domain") or "")

    for col in column_defs:
        col_name = str(col.get("name") or "").strip()
        ctype = str(col.get("type") or "").strip().lower()
        if ctype in _BLOB_TYPES:
            out.append(
                AnomalyFlag(
                    level="BLOCK",
                    name="unsupported_blob_type",
                    reason=f"Column {col_name} has unsupported type {ctype.upper()}",
                )
            )

    this_types = cluster_column_types.get(table_key, {})
    for col_name, ctype in this_types.items():
        for other_table, other_types in cluster_column_types.items():
            if other_table == table_key:
                continue
            other_type = other_types.get(col_name)
            if other_type and other_type != ctype:
                out.append(
                    AnomalyFlag(
                        level="BLOCK",
                        name="cluster_type_inconsistency",
                        reason=(
                            f"Column {col_name}: {ctype.upper()} in {table_key} vs "
                            f"{other_type.upper()} in {other_table}"
                        ),
                    )
                )
                break

    counts: list[int] = []
    for row in cluster_rows:
        try:
            counts.append(int(row.get("column_count") or 0))
        except (TypeError, ValueError):
            counts.append(0)
    n = len(column_defs)
    if counts and any(c > 0 for c in counts):
        mu = mean(counts)
        sigma = pstdev(counts) if len(counts) > 1 else 0.0
        if sigma > 0 and n > mu + 2 * sigma:
            z = (n - mu) / sigma
            out.append(
                AnomalyFlag(
                    level="WARN",
                    name="column_count_outlier",
                    reason=f"Column count {n} is {z:.1f}σ above cluster mean {mu:.0f}",
                )
            )

    sample_rows = inventory_row.get("sample_rows")
    if isinstance(sample_rows, list) and sample_rows:
        nulls = 0
        total = 0
        for row in sample_rows:
            if not isinstance(row, dict):
                continue
            for value in row.values():
                total += 1
                if value is None:
                    nulls += 1
        if total > 0 and (nulls / total) > 0.5:
            out.append(
                AnomalyFlag(
                    level="WARN",
                    name="high_null_rate",
                    reason=f"NULL rate is {(100.0 * nulls / total):.1f}% in available sample rows",
                )
            )
    else:
        out.append(
            AnomalyFlag(
                level="INFO",
                name="null_rate_check_skipped",
                reason="null rate check skipped because sample_rows are unavailable",
            )
        )

    snake_like = 0
    mixed_like = 0
    for col in column_defs:
        name = str(col.get("name") or "")
        if "_" in name and name.lower() == name:
            snake_like += 1
        elif any(ch.isupper() for ch in name):
            mixed_like += 1
    if snake_like > 0 and mixed_like > 0:
        out.append(
            AnomalyFlag(
                level="INFO",
                name="naming_entropy",
                reason=f"Mixed naming convention in cluster {domain} for table {table_key}",
            )
        )
    return out
