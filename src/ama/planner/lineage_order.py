"""
Lineage-aware ordering for migration planning.

The report ``lineage.edges`` list comes from :class:`ama.lineage.LineageGraph`: **undirected**
co-query weights (both directions stored). We **orient** each pair using a total order on
inventory tables — **ascending** ``(priority_score, full_name)`` — so the lower-priority
endpoint is treated as migrating **before** the higher-priority one (typical for dimension /
source vs fact / heavy consumer in the same query). That yields a **DAG** (subgraph of the
total order). We then run **Kahn** topological sort; when several nodes are ready, we pick
the **highest** ``priority_score`` first (business-critical when the DAG allows).

With **no** lineage edges, ordering collapses to **descending** priority (same as the legacy
planner behavior).
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Any


def _priority_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        fn = str(r.get("full_name") or "").strip()
        if not fn:
            continue
        try:
            out[fn] = float(r.get("priority_score") or 0.0)
        except (TypeError, ValueError):
            out[fn] = 0.0
    return out


def _undirected_pairs_from_edges(
    edges: list[Any],
    allowed: set[str],
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "") or "").strip()
        b = str(e.get("to", "") or "").strip()
        if not a or not b or a == b:
            continue
        if a not in allowed or b not in allowed:
            continue
        pairs.add((a, b) if a < b else (b, a))
    return pairs


def _rank_ascending(names: list[str], priority: dict[str, float]) -> dict[str, int]:
    """Lower (priority_score, name) = earlier in the orientation backbone."""
    sorted_n = sorted(names, key=lambda n: (priority.get(n, 0.0), n.lower()))
    return {n: i for i, n in enumerate(sorted_n)}


def _orient_pairs_to_dag(
    pairs: set[tuple[str, str]],
    rank_asc: dict[str, int],
) -> list[tuple[str, str]]:
    """Each undirected pair becomes one directed edge following ``rank_asc`` order."""
    dag: list[tuple[str, str]] = []
    for a, b in pairs:
        ra, rb = rank_asc[a], rank_asc[b]
        if ra < rb:
            dag.append((a, b))
        elif rb < ra:
            dag.append((b, a))
        # ra == rb should not happen (distinct names in pair)
    return dag


def _kahn_max_priority(
    nodes: list[str],
    dag_edges: list[tuple[str, str]],
    priority: dict[str, float],
) -> list[str]:
    """Topological order; ready queue ordered by **highest** priority_score first."""
    node_set = set(nodes)
    adj: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {n: 0 for n in nodes}
    for u, v in dag_edges:
        if u not in node_set or v not in node_set:
            continue
        adj[u].append(v)
        indeg[v] = indeg.get(v, 0) + 1
    for n in nodes:
        indeg.setdefault(n, 0)

    heap: list[tuple[float, str]] = []
    for n in nodes:
        if indeg.get(n, 0) == 0:
            heapq.heappush(heap, (-priority.get(n, 0.0), n))

    out: list[str] = []
    while heap:
        _, u = heapq.heappop(heap)
        out.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, (-priority.get(v, 0.0), v))

    if len(out) != len(nodes):
        # Should not happen for DAG from total order; fall back to safe order
        seen = set(out)
        rest = [n for n in nodes if n not in seen]
        rest.sort(key=lambda n: (-priority.get(n, 0.0), n.lower()))
        out.extend(rest)
    return out


def migration_order_full_names(
    rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> tuple[list[str], bool]:
    """
    Return ``full_name`` values in planner order.

    Returns
    -------
    order
        Permutation of inventory ``full_name`` values (only rows with non-empty names).
    lineage_used
        ``True`` if at least one undirected pair from ``report['lineage']['edges']`` linked
        two inventory tables.
    """
    priority = _priority_map(rows)
    names = list(priority.keys())
    if len(names) <= 1:
        return names, False

    lineage = report.get("lineage") if isinstance(report.get("lineage"), dict) else {}
    edges = lineage.get("edges") or []
    allowed = set(names)
    pairs = _undirected_pairs_from_edges(edges, allowed)
    if not pairs:
        order = sorted(names, key=lambda n: (-priority.get(n, 0.0), n.lower()))
        return order, False

    rank_asc = _rank_ascending(names, priority)
    dag = _orient_pairs_to_dag(pairs, rank_asc)
    order = _kahn_max_priority(names, dag, priority)
    return order, True


def sort_rows_by_migration_order(
    rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """
    Sort ``rows`` in place is avoided — return a **new** list sorted by migration order.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return [], False
    order, lineage_used = migration_order_full_names(rows, report)
    pos = {n: i for i, n in enumerate(order)}
    sorted_rows = sorted(
        rows,
        key=lambda r: (pos.get(str(r.get("full_name") or "").strip(), 10**9), str(r.get("full_name", ""))),
    )
    return sorted_rows, lineage_used
