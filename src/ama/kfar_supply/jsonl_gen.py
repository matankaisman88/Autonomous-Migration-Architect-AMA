"""Synthetic SQL log lines matching Kfar Supply distributions (delegates to ``tools/generate_kfar_supply`` when present)."""

from __future__ import annotations

import importlib.util
import random
from collections.abc import Callable
from typing import Any

from ama.config import project_root

from ama.kfar_supply.spec import KFAR_TABLES


def _multitable_fallback_jsonl_lines(n_lines: int, rng: random.Random) -> list[dict[str, str]]:
    """
    When the full ``tools/generate_kfar_supply.py`` bundle is absent (e.g. API Docker image),
    still emit queries touching **every** :data:`KFAR_TABLES` entry plus joins so discovery
    and ``--discovery-merge-all`` populate the full inventory (not ``dbo.orders`` only).
    """
    joins: list[Callable[[], str]] = [
        lambda: (
            f"SELECT o.order_id, i.invoice_id FROM dbo.orders o "
            f"INNER JOIN finance.invoices i ON o.order_id = i.order_id WHERE o.order_id = {rng.randint(1, 50000)}"
        ),
        lambda: (
            f"SELECT ol.line_id, o.order_id FROM dbo.order_lines ol "
            f"JOIN dbo.orders o ON ol.order_id = o.order_id WHERE o.order_id = {rng.randint(1, 50000)}"
        ),
        lambda: (
            f"SELECT s.shipment_id, o.customer_id FROM logistics.shipments s "
            f"INNER JOIN dbo.orders o ON s.order_id = o.order_id WHERE o.order_id = {rng.randint(1, 50000)}"
        ),
        lambda: (
            f"SELECT p.payment_id, i.invoice_id FROM finance.payments p "
            f"JOIN finance.invoices i ON p.invoice_id = i.invoice_id WHERE i.invoice_id = {rng.randint(1, 50000)}"
        ),
        lambda: (
            f"SELECT c.customer_id, o.order_id FROM dbo.customers c "
            f"INNER JOIN dbo.orders o ON c.customer_id = o.customer_id WHERE c.customer_id = {rng.randint(1, 50000)}"
        ),
    ]
    tables = list(KFAR_TABLES)
    out: list[dict[str, str]] = []
    for i in range(n_lines):
        if i % 7 == 0:
            sql = joins[(i // 7) % len(joins)]()
        else:
            t = tables[i % len(tables)]
            rid = rng.randint(1, 50000)
            c0, c1 = t.columns[0], t.columns[1] if len(t.columns) > 1 else t.columns[0]
            sql = f"SELECT {c0}, {c1} FROM {t.schema_name}.{t.table_name} WHERE {t.primary_key} = {rid}"
        dialect = "snowflake" if rng.random() < 0.05 else "tsql"
        out.append({"env": "prod", "dialect": dialect, "sql": sql})
    return out


def build_jsonl_lines(n_lines: int, *, seed: int | None = None) -> list[dict[str, str]]:
    """
    Build ``n_lines`` JSONL row dicts (``env``, ``dialect``, ``sql``).

    Uses the canonical generator in ``tools/generate_kfar_supply.py`` when the repo
    layout is available; otherwise uses :func:`_multitable_fallback_jsonl_lines` so Docker
    and minimal installs still exercise the full Kfar DDL manifest.
    """
    rng = random.Random(seed if seed is not None else 42)
    path = project_root() / "tools" / "generate_kfar_supply.py"
    if not path.is_file():
        return _multitable_fallback_jsonl_lines(n_lines, rng)
    spec = importlib.util.spec_from_file_location("_ama_kfar_tool", path)
    if spec is None or spec.loader is None:
        return _multitable_fallback_jsonl_lines(n_lines, rng)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn: Any = getattr(mod, "_build_jsonl_lines", None)
    if callable(fn):
        return fn(rng, n_lines)
    return _multitable_fallback_jsonl_lines(n_lines, rng)
