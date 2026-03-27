"""Regression tests for alias-free table keys and bare-schema filtering."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from ama.parsing.backend import SqlGlotParseBackend
from ama.discovery import build_discovery_payload
from ama.parsing.sqlglot_extract import _resolve_aliases, qualified_key_from_table
from ama.sql_pipeline import TableColumnStats


def _parse(sql: str, dialect: str = "tsql"):
    return sqlglot.parse_one(sql, dialect=dialect)


def _parse_select(sql: str, dialect: str = "tsql") -> exp.Expression:
    return _parse(sql, dialect=dialect)


def test_no_alias_tsql() -> None:
    parsed = _parse_select("SELECT * FROM finance.payments")
    tables = list(parsed.find_all(exp.Table))
    assert qualified_key_from_table(tables[0]) == "finance.payments"


def test_alias_as_p_tsql() -> None:
    """AS p must NOT appear in the key."""
    parsed = _parse_select("SELECT p.payment_id FROM finance.payments AS p")
    tables = list(parsed.find_all(exp.Table))
    key = qualified_key_from_table(tables[0])
    assert key == "finance.payments", f"Got: {key!r}"
    assert "as" not in key.lower()
    assert "_as_" not in key


def test_implicit_alias_tsql() -> None:
    """Implicit alias (no AS keyword) must also not appear in key."""
    parsed = _parse_select("SELECT p.id FROM finance.payments p")
    tables = list(parsed.find_all(exp.Table))
    key = qualified_key_from_table(tables[0])
    assert key == "finance.payments", f"Got: {key!r}"


def test_join_two_aliased_tables() -> None:
    sql = (
        "SELECT i.invoice_id, p.payment_id "
        "FROM finance.invoices AS i "
        "INNER JOIN finance.payments AS p ON p.invoice_id = i.invoice_id"
    )
    parsed = _parse_select(sql)
    keys = {qualified_key_from_table(t) for t in parsed.find_all(exp.Table)}
    assert "finance.invoices" in keys
    assert "finance.payments" in keys
    assert not any("_as_" in k for k in keys), f"Alias leaked: {keys}"


def test_three_part_name_no_alias() -> None:
    parsed = _parse_select("SELECT * FROM MyDB.dbo.orders", dialect="tsql")
    tables = list(parsed.find_all(exp.Table))
    key = qualified_key_from_table(tables[0])
    assert key == "mydb.dbo.orders"


def test_alias_as_p_excluded_from_key() -> None:
    parsed = _parse("SELECT p.payment_id FROM finance.payments AS p")
    keys = [qualified_key_from_table(t) for t in parsed.find_all(exp.Table)]
    assert "finance.payments" in keys
    assert not any("_as_" in k for k in keys), f"Alias leaked: {keys}"


def test_implicit_alias_excluded() -> None:
    parsed = _parse("SELECT p.id FROM finance.payments p")
    keys = [qualified_key_from_table(t) for t in parsed.find_all(exp.Table)]
    assert "finance.payments" in keys
    assert all("." in k or not k for k in keys)


def test_resolve_aliases_maps_tsql_alias() -> None:
    sql = (
        "SELECT p.payment_id, i.invoice_id "
        "FROM finance.payments AS p "
        "JOIN finance.invoices AS i ON p.invoice_id = i.invoice_id"
    )
    parsed = _parse_select(sql)
    sel = next(parsed.find_all(exp.Select))
    alias_map, _ = _resolve_aliases(sel)
    assert alias_map.get("p") == "finance.payments", f"alias_map={alias_map}"
    assert alias_map.get("i") == "finance.invoices", f"alias_map={alias_map}"


def test_build_discovery_payload_filters_bare_schema() -> None:
    """Bare schema-name keys must not appear in the inventory."""
    tables = {
        "dbo.orders": TableColumnStats(query_count=100),
        "dbo": TableColumnStats(query_count=5),
        "finance": TableColumnStats(query_count=2),
        "finance.invoices": TableColumnStats(query_count=80),
    }
    payload = build_discovery_payload(
        tables,
        "dbo.orders",
        "dbo.orders",
        None,
    )
    full_names = {row["full_name"] for row in payload["inventory"]}
    assert "dbo" not in full_names, "Bare schema 'dbo' must be filtered from inventory"
    assert "finance" not in full_names, "Bare schema 'finance' must be filtered from inventory"
    assert "dbo.orders" in full_names
    assert "finance.invoices" in full_names


def test_lineage_graph_excludes_bare_schema_keys() -> None:
    """Bare schema names (no dot) must not appear as lineage graph nodes or edges."""
    from ama.lineage import LineageGraph
    from ama.parsing.backend import ParseResult

    graph = LineageGraph()

    # Simulate a ParseResult where the chunk map contains a bare schema key
    # (as produced before the qualified_key_from_table fix for edge cases)
    pr = ParseResult(
        chunks=[{
            "dbo": {"select:id": 1},          # bare schema token — must be ignored
            "dbo.orders": {"select:id": 1},    # real table key — must be kept
            "finance.invoices": {"select:order_id": 1},  # real table key — must be kept
        }],
        mode="regex",
        expression=None,
    )
    graph.ingest_parse_result(pr)

    report = graph.to_report_dict()
    edge_endpoints = {e["from"] for e in report["edges"]} | {e["to"] for e in report["edges"]}

    assert "dbo" not in edge_endpoints, (
        f"Bare schema 'dbo' must not appear in lineage edges. Endpoints: {edge_endpoints}"
    )
    assert "dbo.orders" in edge_endpoints, "Real table 'dbo.orders' must be present"
    assert "finance.invoices" in edge_endpoints, "Real table 'finance.invoices' must be present"
    assert report["edge_count_undirected"] == 1  # only dbo.orders <-> finance.invoices


def test_lineage_graph_real_pairs_unaffected() -> None:
    """The dot-filter must not remove valid schema.table keys."""
    from ama.lineage import LineageGraph
    from ama.parsing.backend import ParseResult

    graph = LineageGraph()
    pr = ParseResult(
        chunks=[{
            "finance.invoices": {"select:id": 1},
            "finance.payments": {"select:invoice_id": 1},
            "dbo.orders": {"select:order_id": 1},
        }],
        mode="regex",
        expression=None,
    )
    graph.ingest_parse_result(pr)
    report = graph.to_report_dict()

    assert report["edge_count_undirected"] == 3  # all three pairs
    endpoints = {e["from"] for e in report["edges"]}
    assert "finance.invoices" in endpoints
    assert "finance.payments" in endpoints
    assert "dbo.orders" in endpoints


def test_regex_fallback_extracts_real_columns(monkeypatch) -> None:
    monkeypatch.setenv("AMA_SQL_PARSE_MODE", "regex")
    backend = SqlGlotParseBackend()
    sql = (
        "SELECT c_0_1, c_1_1, t0.status "
        "FROM sales_core.tbl_00002 t0 "
        "INNER JOIN finance_core.tbl_00004 t1 ON t0.id = t1.parent_id "
        "WHERE t0.shard_key = 1"
    )
    pr = backend.parse(sql, dialect="oracle")
    assert pr.mode == "regex"
    assert pr.chunks
    flat = pr.chunks[0]
    assert "sales_core.tbl_00002" in flat
    cols = set(flat["sales_core.tbl_00002"].keys())
    assert "select:c_0_1" in cols
    assert "select:c_1_1" in cols
    assert "select:status" in cols
    assert "where:shard_key" in cols
    assert "join_on:id" in cols
    assert "select:tbl_00002" not in cols
    assert "select:tbl_00004" not in cols
    assert "finance_core.tbl_00004" in flat
    right_cols = set(flat["finance_core.tbl_00004"].keys())
    assert "join_on:parent_id" in right_cols
    assert "sales_core" not in flat


def test_regex_fallback_insert_columns(monkeypatch) -> None:
    monkeypatch.setenv("AMA_SQL_PARSE_MODE", "regex")
    backend = SqlGlotParseBackend()
    sql = (
        "INSERT INTO finance_core.tbl_00001 (id, parent_id, status, shard_key) "
        "VALUES (1, 0, 'A', 3)"
    )
    pr = backend.parse(sql, dialect="oracle")
    assert pr.mode == "regex"
    assert pr.chunks
    flat = pr.chunks[0]
    assert "finance_core.tbl_00001" in flat
    cols = set(flat["finance_core.tbl_00001"].keys())
    assert "select:id" in cols
    assert "select:parent_id" in cols
    assert "select:status" in cols
    assert "select:shard_key" in cols
