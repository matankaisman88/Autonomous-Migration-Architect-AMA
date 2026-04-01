"""Kfar synthetic SQL log lines touch the full DDL manifest when tools are missing."""

from __future__ import annotations

import random

from ama.kfar_supply.jsonl_gen import _multitable_fallback_jsonl_lines
from ama.kfar_supply.spec import KFAR_TABLES


def test_multitable_fallback_references_every_kfar_table() -> None:
    rng = random.Random(0)
    lines = _multitable_fallback_jsonl_lines(400, rng)
    blob = " ".join(row["sql"] for row in lines).lower()
    for t in KFAR_TABLES:
        needle = f"{t.schema_name}.{t.table_name}".lower()
        assert needle in blob, f"missing inventory key {needle}"
