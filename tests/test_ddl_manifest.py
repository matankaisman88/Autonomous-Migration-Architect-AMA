from __future__ import annotations

from pathlib import Path

from ama.ddl_manifest import load_ddl_manifest, normalize_manifest_table_key, resolve_ddl_path_for_table

ROOT = Path(__file__).resolve().parents[1]


def test_normalize_manifest_table_key_two_segments() -> None:
    assert normalize_manifest_table_key("Sales.Orders").lower() == "sales.orders"


def test_load_ddl_manifest_skips_underscore_keys() -> None:
    p = ROOT / "sample_data" / "ddl" / "ddl_manifest.json"
    if not p.is_file():
        return
    m = load_ddl_manifest(p)
    assert "_comment" not in m
    assert "sales.orders" in m or any("sales.orders" == k.lower() for k in m)


def test_resolve_falls_back_to_default() -> None:
    root = ROOT
    default = (root / "sample_data" / "ddl" / "orders_columns.json").resolve()
    if not default.is_file():
        return
    p = resolve_ddl_path_for_table(
        root,
        {},
        "unknown.schema.table_xyz",
        default_path=default,
    )
    assert p == default


def test_resolve_sales_orders_from_manifest() -> None:
    p = ROOT / "sample_data" / "ddl" / "ddl_manifest.json"
    if not p.is_file():
        return
    m = load_ddl_manifest(p)
    r = resolve_ddl_path_for_table(
        ROOT,
        m,
        "sales.orders",
        default_path=(ROOT / "sample_data" / "ddl" / "orders_columns.json"),
    )
    assert r is not None
    assert r.name == "orders_columns.json"
