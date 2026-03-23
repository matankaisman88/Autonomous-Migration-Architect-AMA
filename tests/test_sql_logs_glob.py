"""Tests for --sql-logs glob expansion (Windows-friendly)."""

from __future__ import annotations

from pathlib import Path

from ama.cli import _expand_explicit_sql_logs


def test_expand_explicit_sql_logs_glob_kfar(tmp_path: Path) -> None:
    """Glob relative to data root resolves to real files."""
    root = Path(__file__).resolve().parents[1]
    paths = _expand_explicit_sql_logs(root, ["sample_data/kfar_supply/sql_logs/*.jsonl"])
    names = {p.name for p in paths}
    assert "kfar_prod.jsonl" in names
