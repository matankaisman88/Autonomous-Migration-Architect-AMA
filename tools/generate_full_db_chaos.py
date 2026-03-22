"""
Tier 5: Full-database chaos SQL log generator for discovery + hierarchical Excel tests.

Creates chaos_data/sql_logs/full_db_chaos.jsonl (10k+ lines), multi-schema tables,
generic column collisions (ID, STATUS, DATA, NAME), cross-schema JOINs, and
encoding/syntax noise (UTF-8, null bytes, broken fragments).

Run from repo root: python tools/generate_full_db_chaos.py
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "chaos_data" / "sql_logs"
# Match IngestionSettings default paths relative to --data-root (sample_data/ddl/...)
GLOSS_SRC = ROOT / "sample_data" / "glossary" / "he_en_columns.json"
GLOSS_DIRTY_SRC = ROOT / "sample_data" / "glossary" / "he_en_columns_dirty.json"
GLOSS_DST = ROOT / "chaos_data" / "sample_data" / "glossary" / "he_en_columns.json"
GLOSS_DIRTY_DST = ROOT / "chaos_data" / "sample_data" / "glossary" / "he_en_columns_dirty.json"
DDL_SRC_DIR = ROOT / "sample_data" / "ddl"
DDL_DST_DIR = ROOT / "chaos_data" / "sample_data" / "ddl"
META_DST = ROOT / "chaos_data" / "sample_data" / "ddl" / "table_metadata.json"

RNG = random.Random(20250322)

# Three schemas, 50+ distinct qualified table names
SCHEMAS = ("PROD_SALES", "LEGACY_HEBREW", "TEMP_JUNK")

TABLES_PROD = [
    "Orders",
    "OrderLines",
    "Customers",
    "Products",
    "Invoices",
    "Shipments",
    "Promotions",
    "Territories",
    "SalesReps",
    "PriceBooks",
    "Contracts",
    "Returns",
    "Credits",
    "TaxRates",
    "Warehouses",
    "InventorySnapshots",
    "ForecastRuns",
    "BudgetLines",
    "Commissions",
    "Targets",
]

TABLES_HE = [
    "טבלת_לקוחות",
    "טבלת_הזמנות",
    "מוצרים",
    "חשבוניות",
    "סטטוסים",
    "נתונים_כלליים",
    "שמות_זמניים",
    "מזהים",
    "כמויות",
    "אזורים",
]

TABLES_TEMP = [f"Tmp_{i}" for i in range(1, 23)]  # 22 ephemeral tables


def _all_tables() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for t in TABLES_PROD:
        rows.append((SCHEMAS[0], t))
    for t in TABLES_HE:
        rows.append((SCHEMAS[1], t))
    for t in TABLES_TEMP:
        rows.append((SCHEMAS[2], t))
    return rows


ALL_TABLES = _all_tables()


def _build_table_metadata() -> dict[str, dict[str, str]]:
    """Rich labels for business-domain clustering and narrative smoke tests."""
    meta: dict[str, dict[str, str]] = {}
    for schema, table in ALL_TABLES:
        key = f"{schema}.{table}"
        if schema == SCHEMAS[0]:
            comment = f"Production sales entity {table}; referenced in revenue and pipeline reporting."
        elif schema == SCHEMAS[1]:
            comment = f"Legacy Hebrew store for {table}; used in regional billing and customer care workflows."
        else:
            comment = "Ephemeral scratch table; safe to drop after cutover."
        meta[key] = {"comment": comment, "business_hint": "legacy" if schema == SCHEMAS[1] else "core"}
    return meta


def _qname(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _row(env: str, dialect: str, sql: str) -> str:
    return json.dumps({"env": env, "dialect": dialect, "sql": sql}, ensure_ascii=False)


def _generic_select(schema: str, table: str) -> str:
    """Same generic columns, different tables — collision stress for AliasResolver."""
    q = _qname(schema, table)
    return (
        f"SELECT ID, STATUS, DATA, NAME, CREATED_AT FROM {q} "
        f"WHERE STATUS = 'open' AND ID > 0"
    )


def _join_cross_schema() -> str:
    a = _qname(SCHEMAS[0], "Orders")
    b = _qname(SCHEMAS[1], "טבלת_לקוחות")
    return (
        f"SELECT o.ID, o.STATUS, c.ID AS CID, c.DATA "
        f"FROM {a} o "
        f"INNER JOIN {b} c ON o.NAME = c.NAME WHERE o.STATUS = N'paid'"
    )


def _broken_fragment() -> str:
    return RNG.choice(
        [
            "SELECT FROM WHERE",
            "SEL\x00ECT 1",
            "SELECT ,,, FROM PROD_SALES.Orders",
            "INSERT INTO TEMP_JUNK.Tmp_1 VALUES (???)",
        ]
    )


def generate_lines(n: int = 10_200) -> list[str]:
    lines: list[str] = []
    dialects = ("postgres", "tsql", "mysql", "snowflake")
    for i in range(n):
        schema, table = RNG.choice(ALL_TABLES)
        d = RNG.choice(dialects)

        if i % 17 == 0:
            lines.append(_row("prod", d, _join_cross_schema()))
        elif i % 23 == 0:
            lines.append(_row("prod", d, _broken_fragment()))
        elif i % 29 == 0:
            # JSON line with embedded null (invalid-ish log row — ingestion should skip)
            bad = '{"env":"prod","dialect":"postgres","sql":"SELECT ID FROM PROD_SALES.Orders WHERE 1=1' + "\x00" + '"}'
            lines.append(bad)
        elif i % 31 == 0:
            # Windows-1255-ish bytes as mojibake in SQL string (still valid JSON)
            sql = "SELECT NAME FROM LEGACY_HEBREW.מוצרים WHERE DATA = N'\u05e9\u05d2\u05d9\u05d0\u05d5\u05ea'"
            lines.append(_row("prod", d, sql))
        elif i % 37 == 0:
            lines.append(_row("prod", d, _generic_select(SCHEMAS[2], RNG.choice(TABLES_TEMP))))
        else:
            lines.append(_row("prod", d, _generic_select(schema, table)))

    return lines


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "full_db_chaos.jsonl"
    lines = generate_lines()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} lines to {path}")

    # DDL + glossary under chaos_data/sample_data/... (matches AMA defaults for --data-root)
    if DDL_SRC_DIR.is_dir():
        DDL_DST_DIR.mkdir(parents=True, exist_ok=True)
        for p in sorted(DDL_SRC_DIR.glob("*.json")):
            shutil.copy2(p, DDL_DST_DIR / p.name)
        print(f"Copied DDL JSON files to {DDL_DST_DIR}")
    if GLOSS_SRC.is_file():
        GLOSS_DST.parent.mkdir(parents=True, exist_ok=True)
        GLOSS_DST.write_bytes(GLOSS_SRC.read_bytes())
        print(f"Copied glossary to {GLOSS_DST}")
    if GLOSS_DIRTY_SRC.is_file():
        GLOSS_DIRTY_DST.parent.mkdir(parents=True, exist_ok=True)
        GLOSS_DIRTY_DST.write_bytes(GLOSS_DIRTY_SRC.read_bytes())
        print(f"Copied dirty glossary to {GLOSS_DIRTY_DST}")
    META_DST.parent.mkdir(parents=True, exist_ok=True)
    META_DST.write_text(
        json.dumps(_build_table_metadata(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote table metadata to {META_DST}")

    print("\nSuggested verification (from repo root):")
    print(
        "  ama-ingest run --data-root ./chaos_data --format excel "
        "--discovery-mode --no-target --skip-vectors -o full_db_report.xlsx"
    )


if __name__ == "__main__":
    main()
