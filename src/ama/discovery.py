"""
Hierarchical schema/table discovery from SQL logs (database.schema.table paths).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from ama.alias_resolver import AliasResolver, MergeResult
from ama.sql_pipeline import TableColumnStats, merge_stats, run_sql_logs_discovery_pipeline, table_matches_target


def split_qualified_name(key: str) -> tuple[str, str, str]:
    """
    Split a qualified table key into (database, schema, table).
    4+ segments: database = first, table = last, schema = middle joined.
    """
    parts = [p for p in key.split(".") if p]
    if len(parts) == 1:
        return "", "", parts[0]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return parts[0], ".".join(parts[1:-1]), parts[-1]


def resolve_target_stats_for_table(
    discovery_tables: dict[str, TableColumnStats],
    target_full_table: str,
) -> tuple[str, TableColumnStats]:
    """Pick the discovered table key that best matches the configured target (highest query volume)."""
    best_k = ""
    best_st = TableColumnStats()
    best_q = -1
    for k, st in discovery_tables.items():
        if table_matches_target(k, target_full_table) and st.query_count >= best_q:
            best_k, best_st, best_q = k, st, st.query_count
    return best_k, best_st


def _inventory_status(
    *,
    is_target: bool,
    stats: TableColumnStats,
    mr: MergeResult | None,
) -> str:
    if stats.query_count <= 0 and not stats.columns:
        return "Empty/Legacy"
    if not is_target:
        return "Discovered (not in DDL scope)"
    if mr is None:
        return "Needs Review (no DDL merge)"
    if mr.review_candidates and not mr.confirmed_entities:
        return "Needs Review"
    if mr.confirmed_entities and not mr.review_candidates:
        return "Ready for Migration"
    if mr.confirmed_entities and mr.review_candidates:
        return "Needs Review"
    if mr.trash_candidates and not mr.confirmed_entities:
        return "Needs Review"
    return "Needs Review"


def build_discovery_payload(
    discovery_tables: dict[str, TableColumnStats],
    target_full_table: str,
    target_key: str,
    mr: MergeResult | None,
    *,
    merge_table_keys: list[str] | None = None,
    multi_table_merge: bool = False,
    merged_summary: dict[str, Any] | None = None,
    default_database: str = "",
) -> dict[str, Any]:
    """Structured discovery block for JSON / Excel (inventory + per-schema breakdown)."""
    merge_keys = set(merge_table_keys or [])
    max_q = max((discovery_tables[k].query_count for k in discovery_tables), default=0)

    inventory: list[dict[str, Any]] = []
    for key in sorted(discovery_tables.keys(), key=lambda k: (-discovery_tables[k].query_count, k)):
        st = discovery_tables[key]
        db, schema, table = split_qualified_name(key)
        if not db and default_database:
            db = default_database
        in_merge = (key in merge_keys) if merge_keys else (bool(target_key) and key == target_key)
        qc = int(st.query_count or 0)
        priority = round(100.0 * qc / max_q, 2) if max_q else 0.0
        if multi_table_merge:
            if in_merge:
                status = "Merged (Top N scope)" if merged_summary else "Needs Review (no DDL merge)"
            else:
                status = "Discovered (outside merge scope)"
        else:
            is_target = bool(target_key) and key == target_key
            status = _inventory_status(is_target=is_target, stats=st, mr=mr if is_target else None)
        if "TEMP" in schema.upper() or "TEMP" in key.upper() or "JUNK" in schema.upper():
            if status.startswith("Discovered") or "outside merge" in status:
                status = "Ephemeral (Temp)"
        inventory.append(
            {
                "database": db,
                "schema": schema,
                "table": table,
                "full_name": key,
                "query_count": qc,
                "column_count": len(st.columns),
                "priority_score": priority,
                "status": status,
            }
        )

    # Migration progress per schema (query-weighted inventory)
    by_schema: dict[str, dict[str, Any]] = {}
    for row in inventory:
        sk = row["schema"] or "(default)"
        if sk not in by_schema:
            by_schema[sk] = {"schema": sk, "table_count": 0, "total_queries": 0}
        by_schema[sk]["table_count"] += 1
        by_schema[sk]["total_queries"] += int(row["query_count"] or 0)

    if mr is not None:
        n_conf = len(mr.confirmed_entities)
        n_rev = len(mr.review_candidates)
        n_trash = len(mr.trash_candidates)
    elif merged_summary:
        n_conf = len(merged_summary.get("merged_entities") or [])
        n_rev = len(merged_summary.get("review_candidates") or [])
        n_trash = len(merged_summary.get("trash_candidates") or [])
    else:
        n_conf = n_rev = n_trash = 0
    for sk, block in by_schema.items():
        if target_key and not multi_table_merge:
            _, tschema, _ = split_qualified_name(target_key)
            block["has_target_table"] = (tschema or "(default)") == sk or (
                sk == "(default)" and not tschema
            )
        elif multi_table_merge and merge_keys:
            block["has_target_table"] = any(
                (split_qualified_name(k)[1] or "(default)") == sk for k in merge_keys
            )
        else:
            block["has_target_table"] = False
        if block.get("has_target_table") and (mr is not None or merged_summary is not None):
            tot = max(n_conf + n_rev + n_trash, 1)
            block["approx_pct_confirmed"] = round(100.0 * n_conf / tot, 2)
            block["approx_pct_review"] = round(100.0 * n_rev / tot, 2)
        else:
            block["approx_pct_confirmed"] = 0.0
            block["approx_pct_review"] = 0.0

    return {
        "enabled": True,
        "target_full_table": target_full_table,
        "target_key": target_key,
        "multi_table_merge": multi_table_merge,
        "merge_table_keys": list(merge_keys) if merge_keys else [],
        "inventory": inventory,
        "schema_breakdown": sorted(by_schema.values(), key=lambda x: (-x["total_queries"], x["schema"])),
        "default_database": default_database or "",
    }


def run_discovery(
    paths: list,
    env: str | None,
    *,
    batch_size: int | None = None,
    progress: bool = False,
    max_records_per_file: int | None = None,
    on_batch_complete: Callable[[int], None] | None = None,
    records_counter: list[int] | None = None,
) -> dict[str, TableColumnStats]:
    """Scan all qualified tables in SQL logs."""
    return run_sql_logs_discovery_pipeline(
        paths,
        env=env,
        batch_size=batch_size,
        progress=progress,
        max_records_per_file=max_records_per_file,
        on_batch_complete=on_batch_complete,
        records_counter=records_counter,
    )


def top_n_tables(discovery_tables: dict[str, TableColumnStats], n: int = 10) -> list[str]:
    """Most active tables by query_count (desc), then name."""
    ranked = sorted(
        discovery_tables.keys(),
        key=lambda k: (-discovery_tables[k].query_count, k),
    )
    return ranked[: max(0, n)]


def _merge_merged_stats_with_prefix(table_key: str, stats: TableColumnStats) -> TableColumnStats:
    """Prefix merged DDL column keys so combined importance stats stay isolated per table."""
    out = TableColumnStats()
    out.query_count = stats.query_count
    for col, cs in stats.columns.items():
        ic = out.columns[f"{table_key}::{col}"]
        ic.select += cs.select
        ic.where += cs.where
        ic.join_on += cs.join_on
        ic.group_by += cs.group_by
        ic.order_by += cs.order_by
    return out


def aggregate_merges_for_tables(
    resolver: AliasResolver,
    discovery_tables: dict[str, TableColumnStats],
    table_keys: list[str],
) -> tuple[dict[str, Any], TableColumnStats]:
    """
    Run AliasResolver.merge_table_stats per table and concatenate results.
    Returns merged_report-shaped dict and combined TableColumnStats for importance (prefixed columns).
    """
    merged_entities: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    trash: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    combined = TableColumnStats()

    for key in table_keys:
        st = discovery_tables.get(key)
        if not st:
            continue
        mr = resolver.merge_table_stats(st, source_table=key)
        sub = _merge_merged_stats_with_prefix(key, mr.merged_stats)
        merge_stats(combined, sub)

        merged_entities.extend(asdict(e) for e in mr.confirmed_entities)
        review.extend(asdict(u) for u in mr.review_candidates)
        trash.extend(asdict(u) for u in mr.trash_candidates)
        proposals.extend(asdict(p) for p in mr.proposals)

    return (
        {
            "merged_entities": merged_entities,
            "review_candidates": review,
            "trash_candidates": trash,
            "merge_proposals": proposals,
        },
        combined,
    )
