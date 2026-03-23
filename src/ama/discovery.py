"""
Hierarchical schema/table discovery from SQL logs (database.schema.table paths).

System-wide migration mode: full log scan, domain clustering, and iterative
global migration state attached to the report (cross-domain lineage preserved).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any, Callable

from ama.alias_resolver import AliasResolver, MergeResult
from ama.lineage import LineageGraph
from ama.planner.lineage_order import sort_rows_by_migration_order
from ama.sql_pipeline import (
    SqlIngestionTelemetry,
    TableColumnStats,
    merge_stats,
    run_sql_logs_discovery_pipeline,
    table_matches_scope,
    table_matches_target,
)


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


def resolve_scope_stats_for_table(
    discovery_tables: dict[str, TableColumnStats],
    migration_context_reference: str,
) -> tuple[str, TableColumnStats]:
    """Pick the discovered table key that best matches the configured scope (highest query volume)."""
    best_k = ""
    best_st = TableColumnStats()
    best_q = -1
    for k, st in discovery_tables.items():
        if table_matches_scope(k, migration_context_reference) and st.query_count >= best_q:
            best_k, best_st, best_q = k, st, st.query_count
    return best_k, best_st


def resolve_target_stats_for_table(
    discovery_tables: dict[str, TableColumnStats],
    target_full_table: str,
) -> tuple[str, TableColumnStats]:
    """Deprecated name for :func:`resolve_scope_stats_for_table`."""
    return resolve_scope_stats_for_table(discovery_tables, target_full_table)


def discovery_scope_primary_key(
    discovery_tables: dict[str, TableColumnStats],
    migration_context_reference: str,
    *,
    fallback_keys: list[str] | None = None,
) -> str:
    """Prefer the discovered key matching ``migration_context_reference``; else first of ``fallback_keys``."""
    for k in discovery_tables:
        if table_matches_scope(k, migration_context_reference):
            return k
    if fallback_keys:
        return fallback_keys[0]
    return ""


def discovery_anchor_key(
    discovery_tables: dict[str, TableColumnStats],
    target_full_table: str,
    *,
    fallback_keys: list[str] | None = None,
) -> str:
    """Deprecated name for :func:`discovery_scope_primary_key`."""
    return discovery_scope_primary_key(
        discovery_tables,
        target_full_table,
        fallback_keys=fallback_keys,
    )


def _inventory_status(
    *,
    is_primary_scope: bool,
    stats: TableColumnStats,
    mr: MergeResult | None,
) -> str:
    if stats.query_count <= 0 and not stats.columns:
        return "Empty/Legacy"
    if not is_primary_scope:
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
    migration_context_reference: str,
    primary_table_key: str,
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
        # Skip bare schema-name tokens (no dot = not a schema.table reference)
        # These appear when the SQL parser encounters schema-qualified aliases and
        # the schema prefix leaks as a standalone key (e.g. "dbo", "finance").
        if "." not in key:
            continue
        st = discovery_tables[key]
        db, schema, table = split_qualified_name(key)
        if not db and default_database:
            db = default_database
        in_merge = (key in merge_keys) if merge_keys else (bool(primary_table_key) and key == primary_table_key)
        qc = int(st.query_count or 0)
        priority = round(100.0 * qc / max_q, 2) if max_q else 0.0
        if multi_table_merge:
            if in_merge:
                status = "Merged (Top N scope)" if merged_summary else "Needs Review (no DDL merge)"
            else:
                status = "Discovered (outside merge scope)"
        else:
            is_primary_scope = bool(primary_table_key) and key == primary_table_key
            status = _inventory_status(is_primary_scope=is_primary_scope, stats=st, mr=mr if is_primary_scope else None)
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
        if primary_table_key and not multi_table_merge:
            _, tschema, _ = split_qualified_name(primary_table_key)
            block["schema_in_merge_scope"] = (tschema or "(default)") == sk or (
                sk == "(default)" and not tschema
            )
        elif multi_table_merge and merge_keys:
            block["schema_in_merge_scope"] = any(
                (split_qualified_name(k)[1] or "(default)") == sk for k in merge_keys
            )
        else:
            block["schema_in_merge_scope"] = False
        if block.get("schema_in_merge_scope") and (mr is not None or merged_summary is not None):
            tot = max(n_conf + n_rev + n_trash, 1)
            block["approx_pct_confirmed"] = round(100.0 * n_conf / tot, 2)
            block["approx_pct_review"] = round(100.0 * n_rev / tot, 2)
        else:
            block["approx_pct_confirmed"] = 0.0
            block["approx_pct_review"] = 0.0

    return {
        "enabled": True,
        "scope_reference": migration_context_reference,
        "primary_table_key": primary_table_key,
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
    telemetry: SqlIngestionTelemetry | None = None,
    lineage: LineageGraph | None = None,
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
        telemetry=telemetry,
        lineage=lineage,
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
    resolver_or_factory: AliasResolver | Callable[[str], AliasResolver],
    discovery_tables: dict[str, TableColumnStats],
    table_keys: list[str],
) -> tuple[dict[str, Any], TableColumnStats]:
    """
    Run AliasResolver.merge_table_stats per table and concatenate results.
    Pass a single :class:`AliasResolver` (same DDL for every table) or a
    ``callable(table_key) -> AliasResolver`` for per-table DDL (see ``ddl_manifest``).
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
        if isinstance(resolver_or_factory, AliasResolver):
            resolver = resolver_or_factory
        else:
            resolver = resolver_or_factory(key)
        mr = resolver.merge_table_stats(st, source_table=key)
        sub = _merge_merged_stats_with_prefix(key, mr.merged_stats)
        merge_stats(combined, sub)

        merged_entities.extend(asdict(e) for e in mr.confirmed_entities)
        review.extend(asdict(u) for u in mr.review_candidates)
        trash.extend(asdict(u) for u in mr.trash_candidates)
        proposals.extend(asdict(p) for p in mr.proposals)

    merged_entities = dedupe_merged_entities(merged_entities)

    return (
        {
            "merged_entities": merged_entities,
            "review_candidates": review,
            "trash_candidates": trash,
            "merge_proposals": proposals,
        },
        combined,
    )


def dedupe_merged_entities(merged_entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Idempotent merge rows: one row per (source_table, canonical_column)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in merged_entities:
        if not isinstance(e, dict):
            continue
        st = str(e.get("source_table") or "").strip()
        col = str(e.get("canonical_column") or "").strip()
        k = (st, col)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _undirected_pairs_with_weights(
    edges: list[Any],
    allowed: set[str],
) -> dict[tuple[str, str], int]:
    """Aggregate co-query weights (undirected)."""
    pair_w: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "") or "").strip()
        b = str(e.get("to", "") or "").strip()
        if not a or not b or a == b:
            continue
        if a not in allowed or b not in allowed:
            continue
        ek = (a, b) if a < b else (b, a)
        w = int(e.get("weight") or 1)
        pair_w[ek] += w
    return dict(pair_w)


def build_coquery_table_clusters(
    lineage_payload: dict[str, Any] | None,
    *,
    inventory_full_names: set[str],
    min_edge_weight: int = 1,
) -> list[dict[str, Any]]:
    """
    Partition tables into connected components from co-query edges (lineage graph).

    Components approximate cross-table affinity clusters for migration planning;
    taxonomy-driven domains are still assigned in :func:`enrich_discovery_business_context`.
    """
    if not lineage_payload or not inventory_full_names:
        return []
    edges = lineage_payload.get("edges") or []
    pair_w = _undirected_pairs_with_weights(edges, inventory_full_names)
    adj: dict[str, list[str]] = defaultdict(list)
    for (a, b), w in pair_w.items():
        if w < min_edge_weight:
            continue
        adj[a].append(b)
        adj[b].append(a)

    visited: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for start in sorted(inventory_full_names):
        if start in visited:
            continue
        stack = [start]
        comp: set[str] = set()
        while stack:
            u = stack.pop()
            if u in visited:
                continue
            visited.add(u)
            comp.add(u)
            for v in adj.get(u, ()):
                if v not in visited:
                    stack.append(v)
        if not comp:
            continue
        tw = 0
        for (x, y), w in pair_w.items():
            if x in comp and y in comp:
                tw += w
        clusters.append(
            {
                "cluster_id": f"coquery_{len(clusters)}",
                "tables": sorted(comp),
                "table_count": len(comp),
                "internal_edge_weight": tw,
            }
        )
        if len(clusters) >= 8000:
            break
    clusters.sort(key=lambda c: (-int(c.get("internal_edge_weight") or 0), -int(c.get("table_count") or 0)))
    return clusters


def finalize_system_migration_discovery(
    discovery: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """
    After business-domain enrichment, attach global migration state: domain iteration order
    (lineage-aware) and co-query clusters. Safe to call repeatedly (deterministic).
    """
    inv = discovery.get("inventory") or []
    if not isinstance(inv, list) or not inv:
        discovery.setdefault(
            "migration_state",
            {
                "mode": "system_wide",
                "domains_detected": [],
                "domain_processing_order": [],
                "coquery_clusters": [],
                "lineage_order_applied": False,
            },
        )
        return

    rows = [r for r in inv if isinstance(r, dict)]
    sorted_rows, lineage_used = sort_rows_by_migration_order(rows, report)
    domain_order: list[str] = []
    seen_d: set[str] = set()
    for r in sorted_rows:
        d = str(r.get("business_domain") or "Unclassified")
        if d not in seen_d:
            seen_d.add(d)
            domain_order.append(d)

    names = {str(r.get("full_name") or "").strip() for r in rows}
    names = {n for n in names if n}
    lineage = report.get("lineage") if isinstance(report.get("lineage"), dict) else None
    clusters = build_coquery_table_clusters(lineage, inventory_full_names=names)

    domains_detected = sorted({str(r.get("business_domain") or "Unclassified") for r in rows})
    merge_scope = report.get("merge_scope") if isinstance(report.get("merge_scope"), dict) else {}
    table_names_merged = list(merge_scope.get("table_names_merged") or [])
    table_names_discovered = sorted(names)
    discovery["migration_state"] = {
        "mode": "system_wide",
        "lineage_order_applied": lineage_used,
        "domains_detected": domains_detected,
        "domain_processing_order": domain_order,
        "coquery_clusters": clusters,
        "table_names_merged": table_names_merged,
        "table_names_discovered": table_names_discovered,
    }


# Back-compat re-exports for tests
__all__ = [
    "aggregate_merges_for_tables",
    "build_coquery_table_clusters",
    "build_discovery_payload",
    "dedupe_merged_entities",
    "discovery_anchor_key",
    "discovery_scope_primary_key",
    "finalize_system_migration_discovery",
    "resolve_scope_stats_for_table",
    "resolve_target_stats_for_table",
    "run_discovery",
    "split_qualified_name",
    "table_matches_target",
    "top_n_tables",
]
