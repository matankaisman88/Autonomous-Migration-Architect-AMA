"""iter_sql_log_records max_records limit."""

from __future__ import annotations

import json
from pathlib import Path

from ama.sql_pipeline import iter_sql_log_records


def test_iter_sql_log_records_respects_max_records(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    lines = [json.dumps({"env": "prod", "sql": f"SELECT {i} FROM a.b"}) for i in range(30)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = list(iter_sql_log_records(p, max_records=7))
    assert len(got) == 7
