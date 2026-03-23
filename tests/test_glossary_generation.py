"""Tests for automated glossary generation (co-occurrence + LLM stub)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ama.glossary import generate_glossary_from_logs
from ama.glossary.cooccurrence import cooccurrence_candidates, mine_cooccurrences


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


# ── unit: co-occurrence mining ────────────────────────────────────────────────


def test_mine_cooccurrences_finds_rtl_ascii_pair(tmp_path: Path) -> None:
    """Hebrew סכום and English amount appearing in the same SELECT should produce a pair."""
    log = tmp_path / "test.jsonl"
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT order_id, סכום, amount FROM dbo.orders WHERE order_id > 0",
            }
        ]
        * 10,
    )
    ddl = ["order_id", "amount", "status", "created_at"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    # סכום should co-occur with amount
    assert "סכום" in pairs, f"Expected 'סכום' in pairs, got: {list(pairs.keys())}"
    assert "amount" in pairs["סכום"], f"Expected 'amount' in סכום pairs: {pairs.get('סכום')}"
    assert pairs["סכום"]["amount"] == 10


def test_mine_cooccurrences_skips_id_columns(tmp_path: Path) -> None:
    """Foreign-key *_id columns must not be collected as adjacency targets."""
    log = tmp_path / "test.jsonl"
    # תאריך_תשלום immediately followed by invoice_id (wrong structural pair)
    # but also followed by paid_at in the next query (correct semantic pair)
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT payment_id, [תאריך_תשלום], invoice_id FROM finance.payments",
            }
        ]
        * 10
        + [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT payment_id, [תאריך_תשלום], paid_at FROM finance.payments",
            }
        ]
        * 5,
    )
    ddl = ["payment_id", "invoice_id", "paid_at", "amount"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    # invoice_id must be skipped (ends in _id)
    assert "invoice_id" not in pairs.get("תאריך_תשלום", {}), (
        "invoice_id must not appear as co-occurrence target (_id columns are structural)"
    )
    # paid_at must be found (correct semantic pair)
    assert "paid_at" in pairs.get("תאריך_תשלום", {}), (
        f"paid_at must be co-occurrence target. Got: {pairs.get('תאריך_תשלום')}"
    )
    assert pairs["תאריך_תשלום"]["paid_at"] == 5


def test_mine_cooccurrences_env_filter(tmp_path: Path) -> None:
    """Rows with env=staging must be excluded when env_filter=prod."""
    log = tmp_path / "test.jsonl"
    _write_jsonl(
        log,
        [
            {
                "env": "staging",
                "dialect": "tsql",
                "sql": "SELECT סכום, amount FROM dbo.orders",
            }
        ]
        * 5,
    )
    ddl = ["amount"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    assert "סכום" not in pairs


def test_mine_cooccurrences_no_rtl_no_output(tmp_path: Path) -> None:
    """Queries with no RTL tokens should produce no co-occurrence pairs."""
    log = tmp_path / "test.jsonl"
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT order_id, amount FROM dbo.orders",
            }
        ]
        * 5,
    )
    ddl = ["order_id", "amount"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    assert len(pairs) == 0


def test_cooccurrence_candidates_min_count_filter(tmp_path: Path) -> None:
    """Pairs below min_count must be excluded from candidates."""
    log = tmp_path / "test.jsonl"
    # 2 occurrences — below min_count=3
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT סכום, amount FROM dbo.orders",
            }
        ]
        * 2,
    )
    ddl = ["amount"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    candidates = cooccurrence_candidates(pairs, min_count=3)
    assert "סכום" not in candidates


def test_cooccurrence_candidates_ranks_by_frequency(tmp_path: Path) -> None:
    """Most frequent co-occurrence should be the top candidate."""
    log = tmp_path / "test.jsonl"
    rows = [{"env": "prod", "dialect": "tsql", "sql": "SELECT סכום, amount FROM dbo.orders"}] * 8 + [
        {"env": "prod", "dialect": "tsql", "sql": "SELECT סכום, status FROM dbo.orders"}
    ] * 3
    _write_jsonl(log, rows)
    ddl = ["amount", "status"]
    pairs = mine_cooccurrences([log], ddl, env_filter="prod")
    candidates = cooccurrence_candidates(pairs, min_count=2, top_k=2)
    assert "סכום" in candidates
    top_ddl, top_count = candidates["סכום"][0]
    assert top_ddl == "amount"
    assert top_count == 8


# ── integration: generate_glossary_from_logs ─────────────────────────────────


def test_generate_glossary_basic(tmp_path: Path) -> None:
    """End-to-end: Hebrew tokens co-occurring with DDL columns become candidates."""
    log = tmp_path / "logs.jsonl"
    rows = []
    for hebrew, english in [("סכום", "amount"), ("סטטוס", "status"), ("תאריך_יצירה", "created_at")]:
        for _ in range(5):
            rows.append(
                {
                    "env": "prod",
                    "dialect": "tsql",
                    "sql": f"SELECT order_id, {hebrew}, {english} FROM dbo.orders",
                }
            )
    _write_jsonl(log, rows)
    ddl = ["order_id", "amount", "status", "created_at", "customer_id"]
    result = generate_glossary_from_logs([log], ddl, llm_enabled=False)

    assert result.rtl_tokens_found >= 3
    assert result.rtl_tokens_resolved >= 3
    assert len(result.candidates) >= 3

    gdict = result.to_glossary_dict()
    assert gdict.get("סכום") == "amount"
    assert gdict.get("סטטוס") == "status"


def test_generate_glossary_to_export_dict_has_meta(tmp_path: Path) -> None:
    """Export dict must contain _meta block."""
    log = tmp_path / "logs.jsonl"
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT order_id, סכום, amount FROM dbo.orders",
            }
        ]
        * 5,
    )
    result = generate_glossary_from_logs([log], ["order_id", "amount"], llm_enabled=False)
    export = result.to_export_dict()
    assert "_meta" in export
    assert "candidates" in export["_meta"]
    assert isinstance(export["_meta"]["candidates"], list)


def test_generate_glossary_no_rtl_produces_warning(tmp_path: Path) -> None:
    """When no RTL tokens are found, result has empty candidates and a warning."""
    log = tmp_path / "logs.jsonl"
    _write_jsonl(
        log,
        [
            {
                "env": "prod",
                "dialect": "tsql",
                "sql": "SELECT order_id, amount FROM dbo.orders",
            }
        ]
        * 5,
    )
    result = generate_glossary_from_logs([log], ["order_id", "amount"], llm_enabled=False)
    assert len(result.candidates) == 0
    # No RTL tokens → rtl_tokens_found = 0 (no warning needed; just empty result)
    assert result.rtl_tokens_found == 0


def test_generate_glossary_confidence_increases_with_frequency(tmp_path: Path) -> None:
    """Higher co-occurrence frequency should produce higher confidence."""
    log = tmp_path / "logs.jsonl"
    # סכום appears with amount 20x, סטטוס with status 4x
    _write_jsonl(
        log,
        [{"env": "prod", "dialect": "tsql", "sql": "SELECT סכום, amount FROM dbo.orders"}] * 20
        + [{"env": "prod", "dialect": "tsql", "sql": "SELECT סטטוס, status FROM dbo.orders"}] * 4,
    )
    ddl = ["amount", "status"]
    result = generate_glossary_from_logs([log], ddl, llm_enabled=False)
    by_term = {c.source_term: c for c in result.candidates}
    assert "סכום" in by_term
    assert "סטטוס" in by_term
    assert by_term["סכום"].confidence > by_term["סטטוס"].confidence


def test_generate_glossary_with_kfar_data() -> None:
    """Smoke test against the real Kfar Supply dataset (skipped if not generated)."""
    root = Path(__file__).resolve().parents[1]
    kfar_log = root / "sample_data" / "kfar_supply" / "sql_logs" / "kfar_prod.jsonl"
    kfar_ddl_json = root / "sample_data" / "kfar_supply" / "ddl" / "dbo_orders.json"
    kfar_manifest = root / "sample_data" / "kfar_supply" / "ddl" / "kfar_manifest.json"

    if not kfar_log.is_file() or not kfar_ddl_json.is_file():
        pytest.skip("Kfar Supply dataset not generated — run tools/generate_kfar_supply.py")

    from ama.alias_resolver import load_ddl_columns
    from ama.ddl_manifest import load_ddl_manifest

    ddl_cols = load_ddl_columns(kfar_ddl_json)
    if kfar_manifest.is_file():
        manifest = load_ddl_manifest(kfar_manifest)
        for _table_key, rel_path in manifest.items():
            p = (root / rel_path).resolve()
            if p.is_file():
                ddl_cols = list(dict.fromkeys(ddl_cols + load_ddl_columns(p)))

    result = generate_glossary_from_logs([kfar_log], ddl_cols, llm_enabled=False)

    # Must find common Hebrew terms from the Kfar glossary
    gdict = result.to_glossary_dict()
    expected = {"סכום": "amount", "סטטוס": "status"}
    for term, col in expected.items():
        assert gdict.get(term) == col, (
            f"Expected {term!r} -> {col!r}, got {gdict.get(term)!r}. "
            f"All candidates: {gdict}"
        )
    assert result.rtl_tokens_resolved >= 3
