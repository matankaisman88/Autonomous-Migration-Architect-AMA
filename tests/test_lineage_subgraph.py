"""Lineage subgraph payload for API / React Flow."""

from __future__ import annotations

from ama.lineage import (
    clear_lineage_adjacency_cache,
    lineage_subgraph_payload,
)


def test_subgraph_self_loop_and_duplicates_ignored() -> None:
    block = {
        "edges": [
            {"from": "dbo.a", "to": "dbo.a", "weight": 5, "kind": "coquery"},
            {"from": "dbo.a", "to": "dbo.b", "weight": 1, "kind": "coquery"},
            {"from": "dbo.b", "to": "dbo.a", "weight": 2, "kind": "coquery"},
            {"from": "dbo.a", "to": "dbo.b", "weight": 10, "kind": "coquery"},
        ]
    }
    out = lineage_subgraph_payload(block, "dbo.a", set())
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"dbo.a", "dbo.b"}
    assert len(out["edges"]) == 1
    assert out["edges"][0]["data"]["weight"] == 10


def test_subgraph_broken_role() -> None:
    block = {"edges": [{"from": "dbo.a", "to": "dbo.b", "weight": 1, "kind": "coquery"}]}
    out = lineage_subgraph_payload(block, "dbo.a", {"dbo.b"})
    roles = {n["id"]: n["data"]["role"] for n in out["nodes"]}
    assert roles["dbo.a"] == "center"
    assert roles["dbo.b"] == "broken"


def test_adjacency_cache_per_report_id() -> None:
    clear_lineage_adjacency_cache()
    block = {"edges": [{"from": "s.t", "to": "s.u", "weight": 3, "kind": "coquery"}]}
    lineage_subgraph_payload(block, "s.t", set(), report_id="r1")
    lineage_subgraph_payload(block, "s.t", set(), report_id="r1")
    clear_lineage_adjacency_cache(report_id="r1")
    lineage_subgraph_payload(block, "s.t", set(), report_id="r1")


def test_empty_lineage() -> None:
    out = lineage_subgraph_payload({}, "dbo.x", set())
    assert out["nodes"] and out["nodes"][0]["id"] == "dbo.x"
    assert out["empty_reason"] == "no_lineage_edges"
