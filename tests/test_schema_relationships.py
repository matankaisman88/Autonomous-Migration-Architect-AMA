"""PK/FK schema relationship graph from DDL manifest."""

from __future__ import annotations

import json
from pathlib import Path

from ama.schema_relationships import (
    _infer_primary_keys,
    build_foreign_key_edges,
    load_ddl_metadata,
    pk_fk_subgraph_payload,
)


def test_infer_primary_keys_single_id_column() -> None:
    pks = _infer_primary_keys("dbo.customers", ["customer_id", "email"], None)
    assert pks == ["customer_id"]


def test_build_foreign_key_edges_from_naming(tmp_path: Path) -> None:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    manifest = {
        "dbo.customers": "ddl/dbo_customers.json",
        "dbo.orders": "ddl/dbo_orders.json",
        "dbo.order_lines": "ddl/dbo_order_lines.json",
    }
    (ddl_dir / "dbo_customers.json").write_text(
        json.dumps({"columns": ["customer_id", "customer_name"]}),
        encoding="utf-8",
    )
    (ddl_dir / "dbo_orders.json").write_text(
        json.dumps({"columns": ["order_id", "customer_id", "amount"]}),
        encoding="utf-8",
    )
    (ddl_dir / "dbo_order_lines.json").write_text(
        json.dumps({"columns": ["line_id", "order_id", "quantity"]}),
        encoding="utf-8",
    )
    edges = build_foreign_key_edges(manifest, tmp_path)
    pairs = {(e.from_table, e.to_table, e.column) for e in edges}
    assert ("dbo.orders", "dbo.customers", "customer_id") in pairs
    assert ("dbo.order_lines", "dbo.orders", "order_id") in pairs


def test_pk_fk_subgraph_center_customers() -> None:
    from ama.schema_relationships import ForeignKeyEdge

    edges = [
        ForeignKeyEdge("dbo.orders", "dbo.customers", "customer_id", "customer_id", "inferred"),
        ForeignKeyEdge("dbo.order_lines", "dbo.orders", "order_id", "order_id", "inferred"),
    ]
    out = pk_fk_subgraph_payload(edges, "dbo.customers", set())
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"dbo.customers", "dbo.orders"}
    assert len(out["edges"]) == 1
    assert out["edges"][0]["source"] == "dbo.orders"
    assert out["edges"][0]["target"] == "dbo.customers"
    assert out["lineage_mode"] == "pk_fk"


def test_pk_fk_subgraph_includes_coquery_stats() -> None:
    from ama.schema_relationships import ForeignKeyEdge

    edges = [
        ForeignKeyEdge("dbo.orders", "dbo.customers", "customer_id", "customer_id", "inferred"),
    ]
    lineage = {
        "edges": [
            {"from": "dbo.orders", "to": "dbo.customers", "weight": 24, "kind": "coquery"},
            {"from": "dbo.customers", "to": "finance.invoices", "weight": 12, "kind": "coquery"},
        ]
    }
    qcounts = {"dbo.customers": 51, "dbo.orders": 88, "finance.invoices": 5}
    out = pk_fk_subgraph_payload(
        edges,
        "dbo.customers",
        set(),
        lineage_block=lineage,
        query_counts=qcounts,
    )
    fk_edge = next(e for e in out["edges"] if e["data"]["kind"] == "pk_fk")
    assert fk_edge["data"]["coquery_count"] == 24
    assert "24 shared queries" in fk_edge["label"]
    co_edge = next(e for e in out["edges"] if e["data"]["kind"] == "coquery")
    assert co_edge["label"] == "12 shared queries"
    center = next(n for n in out["nodes"] if n["id"] == "dbo.customers")
    assert center["data"]["query_count"] == 51


def test_load_ddl_metadata_declared_fks(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text(
        json.dumps(
            {
                "columns": ["order_id", "customer_id"],
                "primary_keys": ["order_id"],
                "foreign_keys": [
                    {
                        "column": "customer_id",
                        "references_table": "dbo.customers",
                        "references_column": "customer_id",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    meta = load_ddl_metadata(p)
    assert meta["primary_keys"] == ["order_id"]
    assert meta["foreign_keys"][0]["references_table"] == "dbo.customers"
