"""
Generate a million-row (or arbitrary size) JSONL SQL log for extreme stress tests.

Streams to disk — does not build the full dataset in RAM.
Each line is a high-complexity query: nested subqueries, 10+ JOINs, long SELECT lists.

Usage:
  python tools/generate_extreme_chaos.py --lines 1000000 --out chaos_data/sql_logs/extreme_1m.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RNG = random.Random(20250322)

SCHEMAS = ("PROD_SALES", "LEGACY_HEBREW", "TEMP_JUNK")
TABLES = [
    "Orders",
    "OrderLines",
    "Customers",
    "Dim_1",
    "Dim_2",
    "Dim_3",
    "Dim_4",
    "Dim_5",
    "Dim_6",
    "Dim_7",
    "Dim_8",
    "Dim_9",
    "Ref_A",
    "Ref_B",
    "טבלת_הזמנות",
    "מוצרים",
]


def _row(env: str, dialect: str, sql: str) -> str:
    return json.dumps({"env": env, "dialect": dialect, "sql": sql}, ensure_ascii=False)


def _complex_select(idx: int) -> str:
    """Deep subquery + 10 INNER JOINs + long identifier list."""
    base = f"PROD_SALES.{RNG.choice(TABLES)}"
    from_clause = f"{base} t0"
    join_sql: list[str] = []
    for j in range(1, 11):
        alias = f"t{j}"
        jt = f"PROD_SALES.{TABLES[j % len(TABLES)]}"
        join_sql.append(
            f"INNER JOIN {jt} {alias} ON t0.ID = {alias}.PARENT_ID AND {alias}.SEQ = {idx % 997}"
        )
    joins = " ".join(join_sql)

    cols = ", ".join(f"c_{k}_{idx % 500}" for k in range(36))
    inner_sq = (
        f"(SELECT {cols} FROM LEGACY_HEBREW.טבלת_הזמנות ih "
        f"WHERE ih.ID IN (SELECT ID FROM {base} WHERE row_num = {idx % 10000}))"
    )
    nested = (
        f"(SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM {inner_sq} iq GROUP BY iq.c_0_{idx % 500}) agg)"
    )
    where = (
        f"t0.STATUS IN ('{idx % 3}', 'x') AND EXISTS (SELECT 1 FROM TEMP_JUNK.Tmp_1 x WHERE x.ID = t0.ID "
        f"AND x.DATA IN (SELECT DATA FROM PROD_SALES.Customers WHERE {nested} > 0))"
    )
    return (
        f"SELECT {cols}, {nested} AS nested_metric FROM {from_clause} {joins} "
        f"WHERE {where} AND t0.ID IN {inner_sq}"
    )


def _alternate_template(idx: int) -> str:
    dialects = ("postgres", "tsql", "mysql", "snowflake")
    d = RNG.choice(dialects)
    return _row("prod", d, _complex_select(idx))


def main() -> None:
    p = argparse.ArgumentParser(description="Generate extreme JSONL SQL logs (streaming).")
    p.add_argument("--lines", type=int, default=1_000_000, help="Number of JSONL rows (default: 1_000_000)")
    p.add_argument(
        "--out",
        type=str,
        default=str(ROOT / "chaos_data" / "sql_logs" / "extreme_1m.jsonl"),
        help="Output path",
    )
    args = p.parse_args()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    n = max(1, int(args.lines))
    written = 0
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for i in range(n):
            f.write(_alternate_template(i))
            f.write("\n")
            written += 1
            if written % 100_000 == 0:
                print(f"  ... {written:,} lines", flush=True)

    print(f"Wrote {written:,} lines to {out}")


if __name__ == "__main__":
    main()
