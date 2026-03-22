"""
Generate high-volume Tier 1/2/3 stress fixtures under sample_data/stress_*.
Run: python tools/generate_stress_samples.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RNG = random.Random(42)


def _w(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(env: str, dialect: str, sql: str) -> str:
    return json.dumps({"env": env, "dialect": dialect, "sql": sql}, ensure_ascii=False)


def tier1_sql_logs(n: int = 400) -> list[str]:
    templates = [
        "SELECT order_id, customer_id, amount FROM sales.orders WHERE status = 'open'",
        "SELECT o.order_id, o.amount, o.status FROM sales.orders o WHERE o.created_at > '2024-01-01'",
        "SELECT COUNT(*) FROM sales.orders WHERE customer_id = 99",
    ]
    return [_row("prod", "postgres", RNG.choice(templates)) for _ in range(n)]


def tier2_sql_logs(n: int = 400) -> list[str]:
    templates = [
        "SELECT [מזהה_הזמנה], [מזהה_לקוח], [סכום] FROM [sales].[orders] WHERE [סטטוס] = N'open'",
        "SELECT order_id, סטטוס FROM sales.orders o WHERE o.amount > 100",
        "SELECT TOP 10 [order_id] FROM [sales].[orders] WHERE [customer_id] IS NOT NULL",
        "SELECT * FROM sales.orders WHERE מזהה_לקוח = 5 AND סטטוס = 'paid'",
    ]
    return [_row("prod", RNG.choice(["tsql", "postgres"]), RNG.choice(templates)) for _ in range(n)]


def tier3_sql_logs(n: int = 400) -> list[str]:
    rows: list[str] = []
    garbage = [
        _row("prod", "postgres", "SELECT ??? FROM nowhere"),
        _row("prod", "postgres", "DROP TABLE ;"),
        '{"broken": true}',
        _row("prod", "postgres", "SELECT flag_1, temp_001, col99 FROM sales.orders WHERE 1=0"),
    ]
    for i in range(n):
        if i % 7 == 0:
            line = (
                '{"env":"prod","dialect":"postgres","sql":"SELECT order_id FROM sales.orders WHERE 1=1'
                + "\x00"
                + '"}'
            )
            rows.append(line)
        elif i % 11 == 0:
            rows.append(RNG.choice(garbage))
        else:
            rows.append(_row("prod", "postgres", "SELECT flag_1, temp_001, col99 FROM sales.orders WHERE 1=0"))
    return rows


def tier4_sql_logs(n: int = 300) -> list[str]:
    """Tier 4: fragmented SQL, broken encodings, noise — should mostly fail DDL mapping."""
    rows: list[str] = []
    chaos_sql = [
        "SEL\x00ECT 1",
        "SELECT FROM sales.orders",
        "SELECT a,,,b FROM sales.orders WHERE created_at 'oops",
        "INSERT INTO sales.orders VALUES (????)",
        "SELECT RANDOM_STRING_XYZ_🔥 FROM sales.orders",
        "SELECT 1; DROP TABLE users; --",
        "SELECT unicode_bad_\uFFFE_col FROM sales.orders",
        "SELECT * FROM sales.orders WHERE col = 'unclosed",
    ]
    for i in range(n):
        if i % 5 == 0:
            rows.append('{"env":"prod","dialect":"postgres","sql": "broken json')
        elif i % 9 == 0:
            rows.append(_row("prod", "postgres", RNG.choice(chaos_sql)))
        else:
            rows.append(
                _row(
                    "prod",
                    "postgres",
                    f"SELECT noise_{i % 17}, zz_{RNG.randint(1, 9999)} FROM sales.orders WHERE 1={i % 2}",
                )
            )
    return rows


def tier_comms(kind: str, n: int = 120) -> list[str]:
    rows = []
    for i in range(n):
        if kind == "tier4_chaos":
            text = "garbage\x00\x01\xff\xfe " + ("sales.orders" if i % 3 else "random_blob")
        elif kind == "tier3_trash":
            text = "noise\x00byte " + "sales.orders"
        elif kind == "tier2_hybrid":
            text = "דוח הזמנות: sales.orders ומזהה לקוח" if i % 2 == 0 else "sales.orders revenue checkpoint"
        else:
            text = "sales.orders revenue checkpoint" if i % 2 == 0 else "Verify customer_id in sales.orders"
        rows.append(json.dumps({"channel": "exec-reporting", "ts": str(i), "text": text}, ensure_ascii=False))
    return rows


def tier_git(name: str) -> str:
    if name == "tier1_clean":
        return """-- clean
SELECT order_id FROM sales.orders;
SELECT customer_id, amount FROM sales.orders WHERE status = 'x';
"""
    if name == "tier2_hybrid":
        return """-- hybrid
SELECT [order_id], [מזהה_לקוח] FROM [sales].[orders];
SELECT סכום, סטטוס FROM sales.orders;
"""
    if name == "tier4_chaos":
        return """-- chaos
SEL ECT 1 FROM sales.orders;
SELECT \x00bad FROM sales.orders;
SELECT nonsense_col_zz FROM sales.orders;
"""
    return """-- trash
SELECT flag_1 FROM sales.orders;
SELECT bad FROM sales.orders;
"""


def main() -> None:
    _w(ROOT / "sample_data" / "stress_tier1" / "sql_logs" / "clean.jsonl", tier1_sql_logs())
    _w(ROOT / "sample_data" / "stress_tier2" / "sql_logs" / "hybrid.jsonl", tier2_sql_logs())
    _w(ROOT / "sample_data" / "stress_tier3" / "sql_logs" / "trash.jsonl", tier3_sql_logs())
    _w(ROOT / "sample_data" / "stress_tier4" / "sql_logs" / "chaos.jsonl", tier4_sql_logs())

    _w(ROOT / "sample_data" / "stress_tier1" / "comms" / "c.jsonl", tier_comms("tier1_clean"))
    _w(ROOT / "sample_data" / "stress_tier2" / "comms" / "c.jsonl", tier_comms("tier2_hybrid"))
    _w(ROOT / "sample_data" / "stress_tier3" / "comms" / "c.jsonl", tier_comms("tier3_trash"))
    _w(ROOT / "sample_data" / "stress_tier4" / "comms" / "c.jsonl", tier_comms("tier4_chaos"))

    for tier, name in (
        ("stress_tier1", "tier1_clean"),
        ("stress_tier2", "tier2_hybrid"),
        ("stress_tier3", "tier3_trash"),
        ("stress_tier4", "tier4_chaos"),
    ):
        p = ROOT / "sample_data" / tier / "git_sql" / "metrics" / f"{name}.sql"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tier_git(name), encoding="utf-8")

    print("Wrote stress fixtures under sample_data/stress_tier{1,2,3,4}/")


if __name__ == "__main__":
    main()
