"""
SQLServerSchemaProvider tests.

These tests use a mocked `pyodbc` module so they can run without a real SQL Server
and without `pyodbc` installed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ama.mcp.base import ExplainResult, TableSchema
from ama.mcp.factory import get_schema_provider
from ama.mcp.pii import mask_row
from ama.mcp.sqlserver_provider import SQLServerSchemaProvider


class MockCursor:
    def __init__(self) -> None:
        self.description = None
        self._results: list[tuple] = []
        self._executed: list[str] = []
        self._showplan_on = False

    @property
    def executed(self) -> list[str]:
        return self._executed

    def execute(self, sql: str, params=None) -> None:
        s = (sql or "").strip()
        low = s.lower()
        self._executed.append(s)

        if low == "set showplan_xml on":
            self._showplan_on = True
            self._results = []
            self.description = None
            return
        if low == "set showplan_xml off":
            self._showplan_on = False
            self._results = []
            self.description = None
            return

        # EXPLAIN: "SET SHOWPLAN_XML ON" compiles and returns XML instead of executing.
        if self._showplan_on:
            self.description = [("QUERY_PLAN", None, None, None, None, None, None)]
            self._results = [("<ShowPlanXML>ok</ShowPlanXML>",)]
            return

        # Discovery: tables
        if "information_schema.tables" in low:
            # (TABLE_SCHEMA, TABLE_NAME)
            self.description = [("TABLE_SCHEMA", None, None, None, None, None, None),
                                 ("TABLE_NAME", None, None, None, None, None, None)]
            self._results = [("dbo", "orders"), ("sales", "customers")]
            return

        # Discovery: columns + pk detection
        if "information_schema.columns" in low and "ordinal_position" in low:
            # (COLUMN_NAME, DATA_TYPE, IS_NULLABLE, IS_PRIMARY_KEY)
            self.description = [("COLUMN_NAME", None, None, None, None, None, None),
                                 ("DATA_TYPE", None, None, None, None, None, None),
                                 ("IS_NULLABLE", None, None, None, None, None, None),
                                 ("IS_PRIMARY_KEY", None, None, None, None, None, None)]
            self._results = [
                ("order_id", "int", "NO", 1),
                ("email", "nvarchar", "YES", 0),
            ]
            return

        # Sampling: SELECT TOP N * FROM [schema].[table]
        if low.startswith("select top"):
            self.description = [("order_id",), ("email",)]
            self._results = [(1001, "alice@example.com")]
            return

        self._results = []
        self.description = None

    def fetchall(self):
        return list(self._results)

    def fetchone(self):
        return self._results[0] if self._results else None


class MockConnection:
    def __init__(self) -> None:
        self._cursor = MockCursor()
        self.autocommit = True
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _install_mock_pyodbc(monkeypatch, *, connect_raises: bool = False) -> MockConnection:
    conn = MockConnection()

    def _connect(_conn_str: str, timeout=None):
        if connect_raises:
            raise RuntimeError("unreachable db")
        return conn

    mock_pyodbc = SimpleNamespace(connect=_connect)
    monkeypatch.setitem(sys.modules, "pyodbc", mock_pyodbc)
    return conn


def test_factory_sqlserver_requires_connection(monkeypatch):
    monkeypatch.delenv("AMA_DB_CONNECTION_STRING", raising=False)
    with pytest.raises(ValueError, match="AMA_SCHEMA_MODE=sqlserver"):
        get_schema_provider(mode="sqlserver")


def test_factory_sqlserver_lazy_pyodbc_import(monkeypatch):
    # Ensure even without pyodbc installed, factory still instantiates the provider.
    monkeypatch.delenv("AMA_DB_CONNECTION_STRING", raising=False)
    if "pyodbc" in sys.modules:
        monkeypatch.delitem(sys.modules, "pyodbc", raising=False)
    monkeypatch.setenv("AMA_DB_CONNECTION_STRING", "DRIVER=Dummy;SERVER=Dummy;DATABASE=Dummy")
    monkeypatch.setenv("AMA_SCHEMA_MODE", "sqlserver")
    provider = get_schema_provider(mode="file")
    assert isinstance(provider, SQLServerSchemaProvider)


def test_sqlserver_unreachable_resilience(monkeypatch):
    _install_mock_pyodbc(monkeypatch, connect_raises=True)
    p = SQLServerSchemaProvider("DRIVER=Dummy;SERVER=Dummy;DATABASE=Dummy", timeout_seconds=2)
    assert p.ping() is False
    assert p.list_tables() == []
    assert p.get_table_schema("dbo.orders") is None
    assert p.get_sample_data("dbo.orders", limit=3) == []


def test_sqlserver_discovery_happy_path(monkeypatch):
    conn = _install_mock_pyodbc(monkeypatch)
    p = SQLServerSchemaProvider("DRIVER=Dummy;SERVER=Dummy;DATABASE=Dummy", timeout_seconds=2)

    assert p.ping() is True

    tables = p.list_tables()
    assert "dbo.orders" in tables
    assert "sales.customers" in tables

    ts = p.get_table_schema("dbo.orders")
    assert isinstance(ts, TableSchema)
    assert ts.schema_name == "dbo"
    assert ts.table_name == "orders"
    assert [c.name for c in ts.columns] == ["order_id", "email"]

    order_id = next(c for c in ts.columns if c.name == "order_id")
    assert order_id.data_type == "int"
    assert order_id.nullable is False
    assert order_id.primary_key is True


def test_sqlserver_sampling_masks_pii(monkeypatch):
    _install_mock_pyodbc(monkeypatch)
    p = SQLServerSchemaProvider("DRIVER=Dummy;SERVER=Dummy;DATABASE=Dummy", timeout_seconds=2)

    rows = p.get_sample_data("dbo.orders", limit=1)
    assert len(rows) == 1
    assert rows[0].data["email"] == mask_row({"email": "alice@example.com"})["email"]
    assert "alice@example.com" not in str(rows[0].data["email"])


def test_sqlserver_execute_explain_returns_xml_and_cleans_up(monkeypatch):
    conn = _install_mock_pyodbc(monkeypatch)
    p = SQLServerSchemaProvider("DRIVER=Dummy;SERVER=Dummy;DATABASE=Dummy", timeout_seconds=2)

    result = p.execute_explain("SELECT 1")
    assert isinstance(result, ExplainResult)
    assert result.ok is True
    assert "<ShowPlanXML>ok</ShowPlanXML>" in result.plan

    executed = conn.cursor().executed
    # Must set showplan on and off (cleanup).
    assert any(x.lower() == "set showplan_xml on" for x in executed)
    assert any(x.lower() == "set showplan_xml off" for x in executed)

