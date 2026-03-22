"""
Bounded table co-occurrence / lineage graph from SQL logs (streaming-safe).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlglot import exp

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
        uq = list(dict.fromkeys(table_keys))
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
