"""
Bounded table co-occurrence / lineage graph from SQL logs (streaming-safe).
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Any

from sqlglot import exp

from ama.ddl_manifest import normalize_manifest_table_key
from ama.parsing.backend import ParseResult
from ama.parsing.sqlglot_extract import qualified_key_from_table

_MAX_TOTAL_EDGES = 60_000
_MAX_WEIGHT_PER_PAIR = 10_000


class LineageGraph:
    """
    Undirected co-query weights between tables (same SQL statement).
    Serialized as directed pairs both ways for downstream BFS if needed.
    """

    def __init__(self, *, max_total_edges: int = _MAX_TOTAL_EDGES) -> None:
        self._max_total = max_total_edges
        # canonical undirected key (min, max) -> weight
        self._pair_w: dict[tuple[str, str], int] = defaultdict(int)

    def ingest_parse_result(self, pr: ParseResult) -> None:
        keys: list[str] = []
        expr = pr.expression
        if expr is not None:
            for t in expr.find_all(exp.Table):
                k = qualified_key_from_table(t)
                if k:
                    keys.append(k)
            keys = list(dict.fromkeys(keys))
        if not keys:
            for flat in pr.chunks:
                for k in flat:
                    if k:
                        keys.append(k)
            keys = list(dict.fromkeys(keys))
        self._add_clique(keys)

    def _add_clique(self, table_keys: list[str]) -> None:
        if len(self._pair_w) >= self._max_total:
            return
        # Exclude bare schema-name tokens (no dot = not a schema.table reference).
        # These leak when the SQL parser encounters schema-qualified aliases and
        # the schema prefix is emitted as a standalone key (e.g. "dbo", "finance").
        uq = list(dict.fromkeys(k for k in table_keys if "." in k))
        n = len(uq)
        for i in range(n):
            for j in range(i + 1, n):
                if len(self._pair_w) >= self._max_total:
                    return
                a, b = uq[i], uq[j]
                if a == b:
                    continue
                ek = (a, b) if a < b else (b, a)
                nw = min(_MAX_WEIGHT_PER_PAIR, self._pair_w[ek] + 1)
                self._pair_w[ek] = nw

    def to_report_dict(self, *, top_edges: int = 4000) -> dict[str, Any]:
        items = sorted(self._pair_w.items(), key=lambda x: -x[1])[:top_edges]
        edges: list[dict[str, Any]] = []
        for (a, b), w in items:
            edges.append({"from": a, "to": b, "weight": int(w), "kind": "coquery"})
            edges.append({"from": b, "to": a, "weight": int(w), "kind": "coquery"})
        return {
            "edge_count_undirected": len(self._pair_w),
            "edges": edges,
        }

    def adjacency(self) -> dict[str, list[tuple[str, int]]]:
        """Weighted undirected adjacency for blast-radius algorithms."""
        adj: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for (a, b), w in self._pair_w.items():
            adj[a].append((b, w))
            adj[b].append((a, w))
        return dict(adj)


# --- Report lineage subgraph (1-hop) for API / React Flow ---------------------------------

_LINEAGE_ADJ_CACHE_LOCK = threading.Lock()
# (report_id, identity of lineage dict) -> undirected adjacency: a -> b -> max_weight
_LINEAGE_ADJ_CACHE: dict[tuple[str, int], dict[str, dict[str, int]]] = {}


def clear_lineage_adjacency_cache(*, report_id: str | None = None) -> None:
    """Drop cached adjacency; if report_id set, only keys with that prefix are removed."""
    with _LINEAGE_ADJ_CACHE_LOCK:
        if report_id is None:
            _LINEAGE_ADJ_CACHE.clear()
            return
        dead = [k for k in _LINEAGE_ADJ_CACHE if k[0] == report_id]
        for k in dead:
            del _LINEAGE_ADJ_CACHE[k]


def _adjacency_from_lineage_block(lineage_block: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """
    Collapse directed report edges into weighted undirected adjacency (max weight per pair).
    Skips self-loops and bare schema keys (no dot).
    """
    lineage_block = lineage_block or {}
    edges = lineage_block.get("edges") or []
    pair_w: dict[tuple[str, str], int] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from", "")).strip(), str(e.get("to", "")).strip()
        if not a or not b or "." not in a or "." not in b:
            continue
        if a == b:
            continue
        w = int(e.get("weight") or 0)
        ek = (a, b) if a < b else (b, a)
        pair_w[ek] = max(pair_w.get(ek, 0), w)
    adj: dict[str, dict[str, int]] = defaultdict(dict)
    for (a, b), w in pair_w.items():
        adj[a][b] = max(adj[a].get(b, 0), w)
        adj[b][a] = max(adj[b].get(a, 0), w)
    return {k: dict(v) for k, v in adj.items()}


def _get_cached_adjacency(report_id: str, lineage_block: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    lb = lineage_block if isinstance(lineage_block, dict) else {}
    key = (report_id, id(lb))
    with _LINEAGE_ADJ_CACHE_LOCK:
        hit = _LINEAGE_ADJ_CACHE.get(key)
        if hit is not None:
            return hit
    adj = _adjacency_from_lineage_block(lb)
    with _LINEAGE_ADJ_CACHE_LOCK:
        _LINEAGE_ADJ_CACHE[key] = adj
    return adj


def _radial_positions(center: str, node_ids: list[str]) -> dict[str, tuple[float, float]]:
    c = center.strip()
    others = sorted(n for n in node_ids if n != c)
    out: dict[str, tuple[float, float]] = {c: (0.0, 0.0)}
    n_other = len(others)
    if n_other == 0:
        return out
    r = max(130.0, min(55.0 * math.sqrt(float(n_other)), 420.0))
    for i, nd in enumerate(others):
        ang = 2.0 * math.pi * (i / n_other) - (math.pi / 2.0)
        out[nd] = (r * math.cos(ang), r * math.sin(ang))
    return out


def _resolve_center_key(center: str, adj: dict[str, dict[str, int]]) -> str:
    """Match ``center`` to a key present in adjacency (normalized + case-insensitive)."""
    if center in adj:
        return center
    nk = normalize_manifest_table_key(center)
    if nk and nk in adj:
        return nk
    lower = center.lower()
    for k in adj:
        if k.lower() == lower:
            return k
    if nk:
        nkl = nk.lower()
        for k in adj:
            if k.lower() == nkl:
                return k
    return center


def lineage_subgraph_payload(
    lineage_block: dict[str, Any] | None,
    center_table_key: str,
    broken_keys: set[str] | frozenset[str] | None,
    *,
    report_id: str | None = None,
) -> dict[str, Any]:
    """
    Build a 1-hop co-query subgraph around ``center_table_key`` for React Flow.

    Roles: ``center`` | ``neighbor`` | ``broken`` (broken = missing DDL manifest).
    Duplicate directed edges and self-loops in the report are ignored safely.
    """
    center_raw = (center_table_key or "").strip()
    broken: set[str] = set(broken_keys or ())

    if not center_raw:
        return {
            "nodes": [],
            "edges": [],
            "empty_reason": "missing_center_table",
        }

    if report_id:
        adj = _get_cached_adjacency(report_id, lineage_block)
    else:
        adj = _adjacency_from_lineage_block(lineage_block)

    center = _resolve_center_key(center_raw, adj)

    neighbors = sorted(adj.get(center, {}).keys())
    if not neighbors and center not in adj and not any(center in adj.get(n, {}) for n in adj):
        # center may still be valid with zero degree
        pass

    node_ids = [center] + [n for n in neighbors if n != center]
    node_ids = list(dict.fromkeys(node_ids))
    positions = _radial_positions(center, node_ids)

    broken_norm = {normalize_manifest_table_key(b) for b in broken}
    nodes: list[dict[str, Any]] = []
    for nid in node_ids:
        nk = normalize_manifest_table_key(nid)
        is_br = nk in broken_norm or nid in broken
        if nid == center:
            role = "broken" if is_br else "center"
        else:
            role = "broken" if is_br else "neighbor"
        x, y = positions.get(nid, (0.0, 0.0))
        nodes.append(
            {
                "id": nid,
                "type": "lineageTable",
                "position": {"x": x, "y": y},
                "data": {"label": nid, "role": role},
            }
        )

    # One undirected edge per pair in subgraph (React Flow: single edge id)
    seen_pairs: set[tuple[str, str]] = set()
    edges_out: list[dict[str, Any]] = []
    for a in node_ids:
        for b, w in adj.get(a, {}).items():
            if b not in node_ids:
                continue
            ek = (a, b) if a < b else (b, a)
            if ek in seen_pairs:
                continue
            seen_pairs.add(ek)
            eid = f"{ek[0]}|{ek[1]}"
            kind = "coquery"
            edges_out.append(
                {
                    "id": eid,
                    "source": ek[0],
                    "target": ek[1],
                    "data": {"weight": int(w), "kind": kind},
                    "label": str(w),
                }
            )

    empty_reason: str | None = None
    if not edges_out and not neighbors:
        empty_reason = "no_edges_for_table" if (lineage_block or {}).get("edges") else "no_lineage_edges"

    return {
        "nodes": nodes,
        "edges": edges_out,
        "empty_reason": empty_reason,
        "center_table_key": center,
    }
