"""Safeguards for oversized / invalid JSONL lines."""

from __future__ import annotations

import json
from pathlib import Path

from ama.sql_pipeline import iter_sql_log_records


def test_iter_sql_log_records_skips_invalid_json_with_warning(tmp_path: Path, capsys) -> None:
    p = tmp_path / "mix.jsonl"
    p.write_text(
        json.dumps({"env": "prod", "sql": "SELECT 1"}) + "\n"
        "not json at all\n"
        + json.dumps({"env": "prod", "sql": "SELECT 2"}) + "\n",
        encoding="utf-8",
    )
    rows = list(iter_sql_log_records(p))
    assert len(rows) == 2
    err = capsys.readouterr().err
    assert "invalid JSON" in err
