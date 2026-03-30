"""
MCP SchemaProvider tests.

All tests must pass without a live database (file provider covers all non-skipped tests).
Live DB tests are skipped unless AMA_TEST_PG_CONN or AMA_TEST_ORA_CONN env vars are set.
"""
from __future__ import annotations

import json
import os
import pytest
from pathlib import Path

from ama.mcp.base import ColumnInfo, ExplainResult, SampleRow, SchemaProvider, TableSchema
from ama.mcp.file_provider import FileSchemaProvider
from ama.mcp.factory import get_schema_provider


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_provider(tmp_path: Path) -> FileSchemaProvider:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "orders.json").write_text(
        json.dumps({"columns": ["order_id", "customer_id", "total_amount", "order_date"]}),
        encoding="utf-8",
    )
    (ddl_dir / "customers.json").write_text(
        json.dumps({"columns": ["customer_id", "name", "email", "phone"]}),
        encoding="utf-8",
    )
    manifest = {
        "sales.orders": "ddl/orders.json",
        "sales.customers": "ddl/customers.json",
    }
    mp = tmp_path / "ddl_manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    return FileSchemaProvider(manifest_path=mp, data_root=tmp_path)


# ── FileSchemaProvider ─────────────────────────────────────────────────────────

def test_file_list_tables(tmp_provider):
    tables = tmp_provider.list_tables()
    assert "sales.orders" in tables
    assert "sales.customers" in tables


def test_file_get_table_list_alias(tmp_provider):
    """get_table_list is the alias used by the discovery endpoint."""
    assert tmp_provider.get_table_list() == tmp_provider.list_tables()


def test_file_schema_filter(tmp_provider):
    assert all(t.startswith("sales.") for t in tmp_provider.list_tables("sales"))
    assert tmp_provider.list_tables("nonexistent") == []


def test_file_get_columns(tmp_provider):
    cols = tmp_provider.get_columns("sales.orders")
    assert "order_id" in cols
    assert "total_amount" in cols


def test_file_get_table_schema(tmp_provider):
    ts = tmp_provider.get_table_schema("sales.orders")
    assert ts is not None
    assert isinstance(ts, TableSchema)
    assert ts.schema_name == "sales"
    assert ts.table_name == "orders"
    assert len(ts.columns) == 4
    assert all(isinstance(c, ColumnInfo) for c in ts.columns)


def test_file_unknown_table(tmp_provider):
    assert tmp_provider.get_table_schema("unknown.table") is None
    assert tmp_provider.get_columns("unknown.table") == []


def test_file_get_sample_data_returns_empty(tmp_provider):
    rows = tmp_provider.get_sample_data("sales.orders", limit=5)
    assert rows == []  # FileSchemaProvider never has live data


def test_file_execute_explain_always_ok(tmp_provider):
    result = tmp_provider.execute_explain("SELECT * FROM sales.orders")
    assert isinstance(result, ExplainResult)
    assert result.ok is True
    assert result.plan == "static_validation_only"
    assert result.dialect == "static"


def test_file_ping(tmp_provider):
    assert tmp_provider.ping() is True


def test_interface_compliance(tmp_provider):
    assert isinstance(tmp_provider, SchemaProvider)
    for method in ("ping", "list_tables", "get_table_list", "get_table_schema",
                   "get_columns", "get_sample_data", "execute_explain"):
        assert callable(getattr(tmp_provider, method))


# ── factory ───────────────────────────────────────────────────────────────────

def test_factory_file_mode(tmp_path):
    mp = tmp_path / "ddl_manifest.json"
    mp.write_text("{}", encoding="utf-8")
    p = get_schema_provider(mode="file", manifest_path=mp, data_root=tmp_path)
    assert isinstance(p, FileSchemaProvider)


def test_factory_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AMA_SCHEMA_MODE", "file")
    mp = tmp_path / "ddl_manifest.json"
    mp.write_text("{}", encoding="utf-8")
    p = get_schema_provider(mode="file", manifest_path=mp, data_root=tmp_path)
    assert isinstance(p, FileSchemaProvider)


def test_factory_postgres_requires_conn(monkeypatch):
    monkeypatch.delenv("AMA_DB_CONNECTION_STRING", raising=False)
    with pytest.raises(ValueError, match="AMA_DB_CONNECTION_STRING"):
        get_schema_provider(mode="postgres")


def test_factory_oracle_requires_conn(monkeypatch):
    monkeypatch.delenv("AMA_DB_CONNECTION_STRING", raising=False)
    with pytest.raises(ValueError, match="AMA_DB_CONNECTION_STRING"):
        get_schema_provider(mode="oracle")


# ── PII masking ───────────────────────────────────────────────────────────────

def test_pii_email_masked():
    from ama.mcp.pii import mask_row
    result = mask_row({"email": "alice@example.com", "amount": 100})
    assert "@example.com" not in result["email"]
    assert result["amount"] == 100


def test_pii_phone_masked():
    from ama.mcp.pii import mask_row
    result = mask_row({"phone": "050-1234567"})
    assert "050-1234567" not in result["phone"]


def test_pii_name_masked():
    from ama.mcp.pii import mask_row
    result = mask_row({"name": "David Cohen"})
    assert result["name"] == "[NAME MASKED]"


def test_pii_non_sensitive_preserved():
    from ama.mcp.pii import mask_row
    result = mask_row({"order_id": "12345", "total": 999.99, "status": "active"})
    assert result["order_id"] == "12345"
    assert result["total"] == 999.99


def test_pii_mask_rows_multiple():
    from ama.mcp.pii import mask_rows
    rows = [
        {"email": "a@b.com", "amount": 1},
        {"email": "c@d.com", "amount": 2},
    ]
    masked = mask_rows(rows)
    assert len(masked) == 2
    assert all("@b.com" not in r["email"] and "@d.com" not in r["email"] for r in masked)


def test_pii_never_crashes():
    from ama.mcp.pii import mask_row
    # Should never raise regardless of input
    result = mask_row({"weird_col": None, "num": 3.14, "empty": ""})
    assert result is not None


# ── encryption ────────────────────────────────────────────────────────────────

def test_encryption_round_trip():
    pytest.importorskip("cryptography")
    from ama.mcp.encryption import generate_key, encrypt, decrypt
    key = generate_key()
    plaintext = "postgresql://user:secret@localhost:5432/mydb"
    token = encrypt(plaintext, key=key)
    assert token != plaintext
    recovered = decrypt(token, key=key)
    assert recovered == plaintext


def test_encryption_mask_connection_string():
    from ama.mcp.encryption import mask_connection_string
    masked = mask_connection_string("postgresql://user:mysecret@host/db")
    assert "mysecret" not in masked
    assert "****" in masked


def test_decryption_bad_token():
    pytest.importorskip("cryptography")
    from ama.mcp.encryption import generate_key, decrypt
    key = generate_key()
    with pytest.raises(ValueError):
        decrypt("not-a-valid-token", key=key)


# ── FastAPI route smoke tests ─────────────────────────────────────────────────

def test_connections_test_endpoint_file_mode(tmp_path):
    from fastapi.testclient import TestClient
    from ama.api.main import app

    mp = tmp_path / "ddl_manifest.json"
    mp.write_text(json.dumps({"sales.orders": "ddl/orders.json"}), encoding="utf-8")

    client = TestClient(app)
    resp = client.post(
        "/api/connections/test",
        json={"mode": "file", "manifest_path": str(mp)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["mode"] == "file"


def test_discovery_tables_endpoint_file_mode(tmp_path):
    from fastapi.testclient import TestClient
    from ama.api.main import app

    mp = tmp_path / "ddl_manifest.json"
    mp.write_text(json.dumps({}), encoding="utf-8")

    client = TestClient(app)
    resp = client.post(
        "/api/discovery/tables",
        json={"mode": "file"},
    )
    assert resp.status_code == 200


def test_connections_explain_file_mode():
    from fastapi.testclient import TestClient
    from ama.api.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/connections/explain",
        json={"sql": "SELECT 1", "mode": "file"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["plan"] == "static_validation_only"


# ── live DB tests (skipped unless env vars set) ───────────────────────────────

PG_CONN = os.environ.get("AMA_TEST_PG_CONN", "")
ORA_CONN = os.environ.get("AMA_TEST_ORA_CONN", "")


@pytest.mark.skipif(not PG_CONN, reason="AMA_TEST_PG_CONN not set")
def test_postgres_ping():
    from ama.mcp.postgres_provider import PostgresSchemaProvider
    p = PostgresSchemaProvider(PG_CONN)
    assert p.ping() is True


@pytest.mark.skipif(not PG_CONN, reason="AMA_TEST_PG_CONN not set")
def test_postgres_list_tables():
    from ama.mcp.postgres_provider import PostgresSchemaProvider
    p = PostgresSchemaProvider(PG_CONN)
    tables = p.list_tables()
    assert isinstance(tables, list)


@pytest.mark.skipif(not PG_CONN, reason="AMA_TEST_PG_CONN not set")
def test_postgres_explain():
    from ama.mcp.postgres_provider import PostgresSchemaProvider
    p = PostgresSchemaProvider(PG_CONN)
    result = p.execute_explain("SELECT 1")
    assert isinstance(result, ExplainResult)


@pytest.mark.skipif(not ORA_CONN, reason="AMA_TEST_ORA_CONN not set")
def test_oracle_ping():
    from ama.mcp.oracle_provider import OracleSchemaProvider
    p = OracleSchemaProvider(ORA_CONN)
    assert p.ping() is True


@pytest.mark.skipif(not ORA_CONN, reason="AMA_TEST_ORA_CONN not set")
def test_oracle_list_tables():
    from ama.mcp.oracle_provider import OracleSchemaProvider
    p = OracleSchemaProvider(ORA_CONN)
    tables = p.list_tables()
    assert isinstance(tables, list)

