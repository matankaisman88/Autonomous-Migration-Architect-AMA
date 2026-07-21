"""Tests for SQL literal redaction and extraction helpers."""

from __future__ import annotations

from ama.mcp.extraction import (
    expand_plan_cache_sql_rows,
    filter_application_sql_texts,
    is_noise_or_system_sql,
    normalize_sql_for_dedupe,
    redact_sql_literals,
    references_user_schema,
    split_tsql_batch,
)


def test_is_noise_or_system_sql() -> None:
    assert is_noise_or_system_sql("sp_helpdb") is True
    assert is_noise_or_system_sql("SELECT * FROM [sys].[objects]") is True
    assert is_noise_or_system_sql("SELECT * FROM dbo.orders WHERE id = 1") is False


def test_references_user_schema() -> None:
    assert references_user_schema("SELECT * FROM dbo.orders", ["dbo"]) is True
    assert references_user_schema("SELECT * FROM [dbo].[orders]", ["dbo"]) is True
    assert references_user_schema("SELECT * FROM sales.customers", ["dbo"]) is False


def test_split_tsql_batch_preserves_block_comments() -> None:
    batch = "USE kfar_supply;\nGO\n/* ama-test-q001 */ SELECT 1 FROM dbo.orders;\nGO\n/* ama-test-q002 */ SELECT 2 FROM dbo.customers;"
    parts = split_tsql_batch(batch)
    assert len(parts) == 2
    assert "ama-test-q001" in parts[0]
    assert "ama-test-q002" in parts[1]


def test_expand_plan_cache_sql_rows() -> None:
    rows = expand_plan_cache_sql_rows(["/* q1 */ SELECT 1 FROM dbo.a;\nGO\n/* q2 */ SELECT 2 FROM dbo.b;"])
    assert len(rows) == 2
    assert "q1" in rows[0] and "q2" in rows[1]


def test_filter_application_sql_texts() -> None:
    raw = [
        "SELECT * FROM [sys].[objects]",
        "SELECT * FROM dbo.orders WHERE id = 1",
        "SELECT * FROM dbo.customers",
    ]
    kept, skipped = filter_application_sql_texts(raw, ["dbo"], max_rows=10)
    assert skipped == 1
    assert len(kept) == 2


def test_redact_sql_literals_strings_and_numbers() -> None:
    raw = "SELECT * FROM sales.orders WHERE email = 'a@b.com' AND amount > 500"
    out = redact_sql_literals(raw)
    assert out == "SELECT * FROM sales.orders WHERE email = '<REDACTED>' AND amount > <N>"


def test_redact_sql_literals_unicode_string() -> None:
    raw = "SELECT * FROM t WHERE name = N'O''Brien'"
    out = redact_sql_literals(raw)
    assert "'<REDACTED>'" in out
    assert "O'Brien" not in out


def test_normalize_sql_for_dedupe_collapses_whitespace() -> None:
    a = normalize_sql_for_dedupe("SELECT  1")
    b = normalize_sql_for_dedupe("select 1")
    assert a == b
