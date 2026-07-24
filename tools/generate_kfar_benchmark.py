#!/usr/bin/env python3
"""
Generate distinct, syntactically valid T-SQL benchmark queries for the local
``kfar_supply`` dev database (six English DDL tables seeded by ``setup_dev_mssql.py``,
plus Legacy Hebrew Views/Synonyms from ``hebrew_invoice_bridge.sql``).

Uses columns from ``src/ama/kfar_supply/spec.py`` / ``sample_data/kfar_supply/ddl/``,
and Hebrew identifiers from the Legacy Bridge (for Live Self-Healing / translation).
Queries are optionally validated against SQL Server via ``MSSQL_CONNECTION_STRING``.

Run from repo root::

    python tools/apply_hebrew_bridge.py            # once after setup_dev_mssql
    python tools/generate_kfar_benchmark.py --count 1000
    python tools/execute_kfar_benchmark.py          # populate Query Store for Live extraction
    python tools/generate_kfar_benchmark.py --count 1000 --jsonl-out live_data/kfar_benchmark/sql_logs/prod.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KFAR_ROOT = ROOT / "sample_data" / "kfar_supply"
DEFAULT_OUT = ROOT / "tools" / "dirty_kfar_queries.sql"
TARGET_DB = "kfar_supply"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ama.kfar_supply.spec import KFAR_TABLES, KfarTable  # noqa: E402


@dataclass(frozen=True)
class SchemaRef:
    table: KfarTable
    alias: str


SCHEMA: dict[str, SchemaRef] = {
    t.full_key: SchemaRef(t, alias)
    for t, alias in zip(
        KFAR_TABLES,
        ("c", "o", "ol", "i", "p", "s"),
        strict=True,
    )
}

C = SCHEMA["dbo.customers"]
O = SCHEMA["dbo.orders"]
OL = SCHEMA["dbo.order_lines"]
I = SCHEMA["finance.invoices"]
P = SCHEMA["finance.payments"]
S = SCHEMA["logistics.shipments"]

# Values that appear in setup_dev_mssql synthetic seed rows.
SEED_STATUS = "active"
SEED_CURRENCY = "USD"
SEED_EMAIL = "alice@example.com"
SEED_COUNTRY = "sample"  # default NVARCHAR fallback from _synthetic_value

ORDER_STATUSES = (SEED_STATUS, "open", "pending", "shipped", "completed", "cancelled")
INVOICE_STATUSES = (SEED_STATUS, "open", "posted", "paid", "void")
PAYMENT_STATUSES = (SEED_STATUS, "posted", "pending", "failed")
SHIPMENT_STATUSES = (SEED_STATUS, "pending", "in_transit", "delivered")
CURRENCIES = (SEED_CURRENCY, "ILS", "EUR", "GBP")
CITIES = ("Tel Aviv", "Haifa", "Jerusalem", "Beer Sheva", "Netanya")
COUNTRIES = (SEED_COUNTRY, "IL", "US", "DE", "GB")


def _tbl(ref: SchemaRef) -> str:
    return f"{ref.table.schema_name}.{ref.table.table_name}"


def _col(ref: SchemaRef, name: str, *, alias: str | None = None) -> str:
    return f"{alias or ref.alias}.{name}"


def _year_filter(rng: random.Random, col: str, year: int) -> str:
    style = rng.randint(0, 2)
    if style == 0:
        return f"YEAR({col}) = {year}"
    if style == 1:
        return f"DATEPART(year, {col}) = {year}"
    return f"CONVERT(VARCHAR(4), {col}, 120) = '{year}'"


def _like_filter(rng: random.Random, col: str, needle: str) -> str:
    if rng.random() < 0.5:
        return f"UPPER({col}) LIKE UPPER(N'%{needle}%')"
    return f"{col} LIKE N'%{needle}%'"


def _status_predicate(rng: random.Random, col: str, values: tuple[str, ...]) -> str:
    pick = rng.sample(values, k=min(rng.randint(1, 3), len(values)))
    if rng.random() < 0.4:
        inner = ", ".join(f"N'{v}'" for v in pick)
        return f"{col} IN ({inner})"
    return f"{col} = N'{rng.choice(values)}'"


def _coalesce_bloat(rng: random.Random, expr: str) -> str:
    cur = expr
    for _ in range(rng.randint(1, 2)):
        cur = f"COALESCE({cur}, {rng.randint(0, 999)})"
    if rng.random() < 0.5:
        cur = f"CASE WHEN {cur} IS NULL THEN 0 WHEN {cur} < 0 THEN 0 ELSE {cur} END"
    return cur


# ---------------------------------------------------------------------------
# Valid T-SQL templates (English DDL columns only; explicit JOIN syntax)
# ---------------------------------------------------------------------------


def tpl_cte_payment_balance(idx: int, rng: random.Random) -> str:
    year = 2020 + (idx % 7)
    min_gap = idx % 500
    return textwrap.dedent(
        f"""
        WITH order_totals AS (
            SELECT { _col(O, 'order_id') }, { _col(O, 'customer_id') },
                   { _coalesce_bloat(rng, _col(O, 'amount')) } AS order_amt,
                   { _col(O, 'created_at') }
            FROM { _tbl(O) } { O.alias }
            WHERE 1=1 AND { _year_filter(rng, _col(O, 'created_at'), year) }
        ),
        invoice_roll AS (
            SELECT { _col(I, 'order_id') },
                   SUM({ _col(I, 'net_amount') }) AS inv_net,
                   MAX({ _col(I, 'status') }) AS inv_status
            FROM { _tbl(I) } { I.alias }
            GROUP BY { _col(I, 'order_id') }
        ),
        pay_bal AS (
            SELECT { _col(I, 'order_id') },
                   SUM({ _col(P, 'amount') }) AS paid_amt
            FROM { _tbl(I) } { I.alias }
            INNER JOIN { _tbl(P) } { P.alias } ON { _col(I, 'invoice_id') } = { _col(P, 'invoice_id') }
            GROUP BY { _col(I, 'order_id') }
        )
        SELECT ot.order_id, ot.order_amt, ir.inv_net, pb.paid_amt
        FROM order_totals ot
        LEFT JOIN invoice_roll ir ON ot.order_id = ir.order_id
        LEFT JOIN pay_bal pb ON ot.order_id = pb.order_id
        WHERE EXISTS (
            SELECT 1 FROM { _tbl(P) } px
            INNER JOIN { _tbl(I) } ix ON px.invoice_id = ix.invoice_id
            WHERE ix.order_id = ot.order_id
              AND px.paid_at IS NOT NULL
        )
        AND NOT EXISTS (
            SELECT 1 FROM pay_bal pb2
            WHERE pb2.order_id = ot.order_id
              AND COALESCE(pb2.paid_amt, 0) >= ot.order_amt
        )
        AND ot.order_amt - COALESCE(pb.paid_amt, 0) > {min_gap}
        """
    ).strip()


def tpl_correlated_exists_shipment(idx: int, rng: random.Random) -> str:
    st = SHIPMENT_STATUSES[idx % len(SHIPMENT_STATUSES)]
    return textwrap.dedent(
        f"""
        SELECT { _col(C, 'customer_id') }, { _col(C, 'customer_name') },
               (SELECT COUNT(*) FROM { _tbl(O) } o2
                WHERE o2.customer_id = { _col(C, 'customer_id') }
                  AND { _status_predicate(rng, 'o2.status', ORDER_STATUSES) }) AS order_cnt
        FROM { _tbl(C) } { C.alias }
        WHERE { _like_filter(rng, _col(C, 'customer_name'), rng.choice(CITIES)) }
          AND EXISTS (
                SELECT 1 FROM { _tbl(O) } o
                WHERE o.customer_id = { _col(C, 'customer_id') }
                  AND EXISTS (
                        SELECT 1 FROM { _tbl(S) } { S.alias }
                        WHERE { _col(S, 'order_id') } = o.order_id
                          AND { _col(S, 'shipment_status') } = N'{st}'
                  )
          )
          AND NOT EXISTS (
                SELECT 1 FROM { _tbl(I) } { I.alias }
                WHERE { _col(I, 'order_id') } IN (
                    SELECT ox.order_id FROM { _tbl(O) } ox
                    WHERE ox.customer_id = { _col(C, 'customer_id') }
                )
                AND { _col(I, 'status') } = N'void'
          )
        """
    ).strip()


def tpl_window_running_totals(idx: int, rng: random.Random) -> str:
    part = rng.choice(("customer_id", "currency", "status"))
    rn_fn = rng.choice(("ROW_NUMBER", "DENSE_RANK"))
    return textwrap.dedent(
        f"""
        SELECT { _col(O, 'order_id') }, { _col(O, part) }, { _col(O, 'amount') }, { _col(O, 'created_at') },
               SUM({ _col(O, 'amount') }) OVER (
                   PARTITION BY { _col(O, part) }
                   ORDER BY { _col(O, 'created_at') }
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS run_amt,
               {rn_fn}() OVER (
                   PARTITION BY { _col(O, part) }
                   ORDER BY { _col(O, 'amount') } DESC, { _col(O, 'order_id') }
               ) AS rn
        FROM { _tbl(O) } { O.alias }
        WHERE { _col(O, 'order_id') } >= {idx % 1000}
          AND { _status_predicate(rng, _col(O, 'status'), ORDER_STATUSES) }
        """
    ).strip()


def tpl_rollup_cube_having(idx: int, rng: random.Random) -> str:
    min_orders = 1 + (idx % 3)
    shape = "ROLLUP" if idx % 2 == 0 else "CUBE"
    return textwrap.dedent(
        f"""
        SELECT { _col(O, 'customer_id') }, { _col(O, 'currency') }, { _col(O, 'status') },
               COUNT(DISTINCT { _col(O, 'order_id') }) AS orders,
               SUM({ _coalesce_bloat(rng, _col(O, 'amount')) }) AS gross
        FROM { _tbl(O) } { O.alias }
        INNER JOIN { _tbl(C) } { C.alias } ON { _col(C, 'customer_id') } = { _col(O, 'customer_id') }
        WHERE 1=1
          AND ({ _col(C, 'country_code') } = N'{COUNTRIES[idx % len(COUNTRIES)]}'
               OR { _col(C, 'country_code') } IS NOT NULL)
        GROUP BY {shape}({ _col(O, 'customer_id') }, { _col(O, 'currency') }, { _col(O, 'status') })
        HAVING COUNT(DISTINCT { _col(O, 'order_id') }) >= {min_orders}
           AND SUM({ _col(O, 'amount') }) >= {idx % 100}
        """
    ).strip()


def tpl_explicit_join_chain(idx: int, rng: random.Random) -> str:
    join_style = idx % 3
    if join_style == 0:
        join_clause = textwrap.dedent(
            f"""
            FROM { _tbl(O) } { O.alias }
            INNER JOIN { _tbl(I) } { I.alias } ON { _col(O, 'order_id') } = { _col(I, 'order_id') }
            LEFT JOIN { _tbl(P) } { P.alias } ON { _col(P, 'invoice_id') } = { _col(I, 'invoice_id') }
            LEFT JOIN { _tbl(S) } { S.alias } ON { _col(S, 'order_id') } = { _col(O, 'order_id') }
            """
        ).strip()
    elif join_style == 1:
        join_clause = textwrap.dedent(
            f"""
            FROM { _tbl(O) } { O.alias }
            LEFT JOIN { _tbl(I) } { I.alias } ON { _col(O, 'order_id') } = { _col(I, 'order_id') }
            LEFT JOIN { _tbl(P) } { P.alias } ON { _col(P, 'invoice_id') } = { _col(I, 'invoice_id') }
            FULL OUTER JOIN { _tbl(S) } { S.alias } ON { _col(S, 'order_id') } = { _col(O, 'order_id') }
            """
        ).strip()
    else:
        join_clause = textwrap.dedent(
            f"""
            FROM { _tbl(C) } { C.alias }
            RIGHT JOIN { _tbl(O) } { O.alias } ON { _col(C, 'customer_id') } = { _col(O, 'customer_id') }
            INNER JOIN { _tbl(I) } { I.alias } ON { _col(O, 'order_id') } = { _col(I, 'order_id') }
            LEFT JOIN { _tbl(S) } { S.alias } ON { _col(S, 'order_id') } = { _col(O, 'order_id') }
            """
        ).strip()
    return textwrap.dedent(
        f"""
        SELECT { _col(O, 'order_id') }, { _col(I, 'invoice_id') }, { _col(P, 'payment_id') },
               { _col(S, 'tracking_number') }, { _col(C, 'customer_name') }
        {join_clause}
        WHERE { _year_filter(rng, _col(I, 'created_at'), 2020 + idx % 7) }
          AND { _col(O, 'amount') } + 0 > {idx % 100}
        """
    ).strip()


def tpl_order_lines_agg(idx: int, rng: random.Random) -> str:
    prod = 1000 + (idx % 9000)
    return textwrap.dedent(
        f"""
        WITH line_agg AS (
            SELECT { _col(OL, 'order_id') },
                   SUM({ _col(OL, 'quantity') } * { _col(OL, 'unit_price') }) AS ext_price,
                   SUM({ _col(OL, 'net_amount') }) AS net_sum
            FROM { _tbl(OL) } { OL.alias }
            WHERE { _col(OL, 'product_id') } = {prod}
            GROUP BY { _col(OL, 'order_id') }
        )
        SELECT { _col(O, 'order_id') }, { _col(O, 'amount') }, la.ext_price, la.net_sum
        FROM { _tbl(O) } { O.alias }
        INNER JOIN line_agg la ON la.order_id = { _col(O, 'order_id') }
        WHERE ABS({ _col(O, 'amount') } - la.ext_price) >= {idx % 25}
           OR { _col(O, 'discount') } IS NOT NULL
        """
    ).strip()


def tpl_invoice_payment_gap(idx: int, rng: random.Random) -> str:
    gap = idx % 200
    return textwrap.dedent(
        f"""
        SELECT { _col(I, 'invoice_id') }, { _col(I, 'order_id') }, { _col(I, 'amount') },
               (SELECT SUM(p2.amount) FROM { _tbl(P) } p2
                WHERE p2.invoice_id = { _col(I, 'invoice_id') }) AS paid
        FROM { _tbl(I) } { I.alias }
        WHERE { _status_predicate(rng, _col(I, 'status'), INVOICE_STATUSES) }
          AND { _col(I, 'amount') } - COALESCE((
                SELECT SUM(p3.amount) FROM { _tbl(P) } p3
                WHERE p3.invoice_id = { _col(I, 'invoice_id') }
          ), 0) >= {gap}
          AND { _year_filter(rng, _col(I, 'due_date'), 2019 + idx % 8) }
        """
    ).strip()


def tpl_customer_active_noise(idx: int, rng: random.Random) -> str:
    active_val = idx % 2
    return textwrap.dedent(
        f"""
        SELECT { _col(C, 'customer_id') }, { _col(C, 'customer_name') }, { _col(C, 'email') },
               CASE WHEN { _col(C, 'is_active') } = {active_val} THEN N'Y'
                    WHEN TRY_CAST({ _col(C, 'is_active') } AS INT) = {active_val} THEN N'Y2'
                    ELSE N'N' END AS active_flag
        FROM { _tbl(C) } { C.alias }
        WHERE { _like_filter(rng, _col(C, 'city'), rng.choice(CITIES)) }
          AND ({ _col(C, 'country_code') } = N'{COUNTRIES[idx % len(COUNTRIES)]}'
               OR { _col(C, 'email') } LIKE N'%@%')
          AND 1=1
        """
    ).strip()


def tpl_shipment_kpi_nested(idx: int, rng: random.Random) -> str:
    wh = 1 + (idx % 20)
    return textwrap.dedent(
        f"""
        WITH ship_rank AS (
            SELECT { _col(S, 'shipment_id') }, { _col(S, 'order_id') }, { _col(S, 'shipped_at') },
                   { _col(S, 'warehouse_id') },
                   ROW_NUMBER() OVER (
                       PARTITION BY { _col(S, 'order_id') }
                       ORDER BY { _col(S, 'shipped_at') } DESC
                   ) AS rn
            FROM { _tbl(S) } { S.alias }
            WHERE { _col(S, 'warehouse_id') } = {wh}
        )
        SELECT { _col(O, 'order_id') }, { _col(O, 'customer_id') }, sr.shipment_id, sr.rn
        FROM { _tbl(O) } { O.alias }
        INNER JOIN ship_rank sr ON sr.order_id = { _col(O, 'order_id') } AND sr.rn = 1
        WHERE { _col(O, 'sales_rep_id') } IS NOT NULL
          AND { _status_predicate(rng, _col(O, 'status'), ORDER_STATUSES) }
          AND EXISTS (
              SELECT 1 FROM { _tbl(I) } { I.alias }
              WHERE { _col(I, 'order_id') } = { _col(O, 'order_id') }
                AND { _col(I, 'vat_rate') } >= {0.01 * (idx % 9 + 1)}
          )
        """
    ).strip()


def tpl_payment_status_currency(idx: int, rng: random.Random) -> str:
    cur = CURRENCIES[idx % len(CURRENCIES)]
    pst = PAYMENT_STATUSES[idx % len(PAYMENT_STATUSES)]
    return textwrap.dedent(
        f"""
        SELECT { _col(P, 'payment_id') }, { _col(P, 'invoice_id') }, { _col(P, 'amount') },
               { _col(P, 'paid_at') }, { _col(P, 'payment_status') }, { _col(P, 'currency') },
               { _col(I, 'status') } AS invoice_status
        FROM { _tbl(P) } { P.alias }
        INNER JOIN { _tbl(I) } { I.alias } ON { _col(P, 'invoice_id') } = { _col(I, 'invoice_id') }
        WHERE { _col(P, 'currency') } = N'{cur}'
          AND { _col(P, 'payment_status') } = N'{pst}'
          AND CONVERT(VARCHAR(10), { _col(P, 'paid_at') }, 120) >= '202{idx % 7}-01-01'
        """
    ).strip()


def tpl_multi_cte_cross_schema(idx: int, rng: random.Random) -> str:
    return textwrap.dedent(
        f"""
        WITH c_base AS (
            SELECT { _col(C, 'customer_id') }, { _col(C, 'customer_name') }
            FROM { _tbl(C) } { C.alias }
            WHERE TRY_CAST({ _col(C, 'is_active') } AS INT) = {idx % 2}
        ),
        o_base AS (
            SELECT { _col(O, 'order_id') }, { _col(O, 'customer_id') }, { _col(O, 'amount') },
                   { _col(O, 'created_at') }
            FROM { _tbl(O) } { O.alias }
            WHERE { _year_filter(rng, _col(O, 'created_at'), 2021 + idx % 5) }
        ),
        fin AS (
            SELECT { _col(I, 'order_id') }, SUM({ _col(I, 'net_amount') }) AS inv_net
            FROM { _tbl(I) } { I.alias }
            GROUP BY { _col(I, 'order_id') }
        ),
        logi AS (
            SELECT { _col(S, 'order_id') }, MAX({ _col(S, 'shipment_status') }) AS last_ship_st
            FROM { _tbl(S) } { S.alias }
            GROUP BY { _col(S, 'order_id') }
        )
        SELECT cb.customer_id, cb.customer_name, ob.order_id,
               ob.amount, fin.inv_net, logi.last_ship_st
        FROM c_base cb
        INNER JOIN o_base ob ON cb.customer_id = ob.customer_id
        LEFT JOIN fin ON fin.order_id = ob.order_id
        LEFT JOIN logi ON logi.order_id = ob.order_id
        WHERE COALESCE(fin.inv_net, 0) <= ob.amount + {idx % 17}
        """
    ).strip()


def tpl_dense_rank_lines(idx: int, rng: random.Random) -> str:
    mod = 2 + idx % 7
    rem = idx % mod
    return textwrap.dedent(
        f"""
        SELECT { _col(OL, 'line_id') }, { _col(OL, 'order_id') }, { _col(OL, 'product_id') },
               { _col(OL, 'quantity') }, { _col(OL, 'unit_price') },
               DENSE_RANK() OVER (
                   PARTITION BY { _col(OL, 'order_id') }
                   ORDER BY { _col(OL, 'net_amount') } DESC
               ) AS line_rank,
               SUM({ _col(OL, 'quantity') }) OVER (
                   PARTITION BY { _col(OL, 'product_id') }
                   ORDER BY { _col(OL, 'order_id') }
               ) AS prod_running_qty
        FROM { _tbl(OL) } { OL.alias }
        WHERE { _col(OL, 'unit_price') } * { _col(OL, 'quantity') } <> COALESCE({ _col(OL, 'net_amount') }, 0)
          AND { _col(OL, 'order_id') } % {mod} = {rem}
        """
    ).strip()


def tpl_not_exists_unpaid(idx: int, rng: random.Random) -> str:
    threshold = idx % 100
    return textwrap.dedent(
        f"""
        SELECT { _col(O, 'order_id') }, { _col(O, 'customer_id') }, { _col(O, 'amount') }
        FROM { _tbl(O) } { O.alias }
        WHERE { _col(O, 'amount') } > {50 + idx % 5000}
          AND NOT EXISTS (
              SELECT 1 FROM { _tbl(I) } { I.alias }
              WHERE { _col(I, 'order_id') } = { _col(O, 'order_id') }
                AND { _col(I, 'status') } = N'paid'
          )
          AND EXISTS (
              SELECT 1 FROM (
                  SELECT { _col(OL, 'order_id') }, SUM({ _col(OL, 'net_amount') }) AS line_total
                  FROM { _tbl(OL) } { OL.alias }
                  GROUP BY { _col(OL, 'order_id') }
              ) lg
              WHERE lg.order_id = { _col(O, 'order_id') }
                AND lg.line_total > {threshold}
          )
        """
    ).strip()


def tpl_full_outer_chain(idx: int, rng: random.Random) -> str:
    return textwrap.dedent(
        f"""
        SELECT COALESCE({ _col(O, 'order_id') }, { _col(I, 'order_id') }, { _col(S, 'order_id') }) AS oid,
               { _col(C, 'customer_name') }, { _col(I, 'net_amount') }, { _col(S, 'tracking_number') }
        FROM { _tbl(C) } { C.alias }
        FULL OUTER JOIN { _tbl(O) } { O.alias } ON { _col(C, 'customer_id') } = { _col(O, 'customer_id') }
        FULL OUTER JOIN { _tbl(I) } { I.alias } ON { _col(O, 'order_id') } = { _col(I, 'order_id') }
        FULL OUTER JOIN { _tbl(S) } { S.alias } ON { _col(S, 'order_id') } = { _col(O, 'order_id') }
        WHERE ({ _col(O, 'currency') } = N'{CURRENCIES[idx % len(CURRENCIES)]}' OR { _col(O, 'currency') } IS NULL)
          AND { _year_filter(rng, 'COALESCE(' + _col(O, 'created_at') + ', ' + _col(I, 'created_at') + ')', 2020 + idx % 7) }
        """
    ).strip()


def tpl_single_table_scan(idx: int, rng: random.Random) -> str:
    tables = (C, O, OL, I, P, S)
    ref = tables[idx % len(tables)]
    col_a = ref.table.columns[idx % len(ref.table.columns)]
    col_b = ref.table.columns[(idx + 1) % len(ref.table.columns)]
    pk = ref.table.primary_key
    if "status" in ref.table.columns:
        pred = _status_predicate(rng, _col(ref, "status"), ORDER_STATUSES)
    else:
        pred = f"{_col(ref, pk)} IS NOT NULL"
    return textwrap.dedent(
        f"""
        SELECT { _col(ref, col_a) }, { _col(ref, col_b) }
        FROM { _tbl(ref) } { ref.alias }
        WHERE { _col(ref, pk) } >= {idx % 1000}
          AND {pred}
        """
    ).strip()


def tpl_cross_join_aggregate(idx: int, rng: random.Random) -> str:
    return textwrap.dedent(
        f"""
        SELECT { _col(C, 'customer_id') }, { _col(O, 'order_id') }, { _col(I, 'invoice_id') },
               { _col(O, 'amount') } AS order_amount,
               { _col(I, 'net_amount') } AS invoice_net
        FROM { _tbl(C) } { C.alias }
        INNER JOIN { _tbl(O) } { O.alias } ON { _col(C, 'customer_id') } = { _col(O, 'customer_id') }
        INNER JOIN { _tbl(I) } { I.alias } ON { _col(O, 'order_id') } = { _col(I, 'order_id') }
        INNER JOIN { _tbl(P) } { P.alias } ON { _col(P, 'invoice_id') } = { _col(I, 'invoice_id') }
        WHERE { _col(O, 'currency') } = N'{CURRENCIES[idx % len(CURRENCIES)]}'
          AND { _col(P, 'amount') } <= { _col(I, 'amount') } + {idx % 50}
        """
    ).strip()


# ---------------------------------------------------------------------------
# Legacy Hebrew bridge templates (requires hebrew_invoice_bridge.sql applied)
# ---------------------------------------------------------------------------


def tpl_hebrew_invoice_bridge(idx: int, rng: random.Random) -> str:
    """Hebrew view columns + join to modern finance.invoices (Self-Healing / translation)."""
    min_amt = idx % 200
    return textwrap.dedent(
        f"""
        SELECT h.[חשבונית], h.[סכום], h.[סכום_נטו], h.[סטטוס], i.invoice_id, i.net_amount
        FROM legacy_hebrew.[חשבוניות] h
        LEFT JOIN finance.invoices i ON h.[חשבונית] = i.invoice_id
        WHERE h.[סכום] IS NOT NULL
          AND COALESCE(h.[סכום], 0) >= {min_amt}
          AND (h.[סטטוס] = N'{INVOICE_STATUSES[idx % len(INVOICE_STATUSES)]}'
               OR i.status = N'{INVOICE_STATUSES[(idx + 1) % len(INVOICE_STATUSES)]}')
        """
    ).strip()


def tpl_hebrew_multi_view_join(idx: int, rng: random.Random) -> str:
    year = 2020 + (idx % 7)
    return textwrap.dedent(
        f"""
        SELECT c.[שם_לקוח], o.[הזמנה], o.[סכום] AS order_amt,
               h.[חשבונית], h.[סכום] AS invoice_amt, s.[מספר_מעקב], s.[סטטוס_משלוח]
        FROM legacy_hebrew.[לקוחות] c
        INNER JOIN legacy_hebrew.[הזמנות] o ON c.[לקוח] = o.[לקוח]
        LEFT JOIN legacy_hebrew.[חשבוניות] h ON o.[הזמנה] = h.[הזמנה]
        LEFT JOIN legacy_hebrew.[משלוחים] s ON o.[הזמנה] = s.[הזמנה]
        WHERE YEAR(o.[תאריך_יצירה]) = {year}
          AND o.[סכום] > {idx % 100}
          AND (s.[סטטוס_משלוח] = N'{SHIPMENT_STATUSES[idx % len(SHIPMENT_STATUSES)]}'
               OR s.[מזהה_משלוח] IS NULL)
        """
    ).strip()


def tpl_hebrew_payments_lines(idx: int, rng: random.Random) -> str:
    return textwrap.dedent(
        f"""
        SELECT ol.[מזהה_שורה], ol.[הזמנה], ol.[כמות], ol.[מחיר_יחידה], ol.[סכום_נטו],
               p.[תשלום], p.[סכום] AS paid_amt, p.[סטטוס_תשלום], h.[חשבונית]
        FROM legacy_hebrew.[שורות_הזמנה] ol
        INNER JOIN legacy_hebrew.[חשבוניות] h ON ol.[הזמנה] = h.[הזמנה]
        LEFT JOIN legacy_hebrew.[תשלומים] p ON h.[חשבונית] = p.[חשבונית]
        WHERE ol.[כמות] >= {(idx % 5) + 1}
          AND COALESCE(p.[סכום], 0) <= COALESCE(h.[סכום], 0) + {idx % 25}
          AND (p.[סטטוס_תשלום] = N'{PAYMENT_STATUSES[idx % len(PAYMENT_STATUSES)]}'
               OR p.[תשלום] IS NULL)
        """
    ).strip()


TEMPLATE_FNS: tuple[Callable[[int, random.Random], str], ...] = (
    tpl_cte_payment_balance,
    tpl_correlated_exists_shipment,
    tpl_window_running_totals,
    tpl_rollup_cube_having,
    tpl_explicit_join_chain,
    tpl_order_lines_agg,
    tpl_invoice_payment_gap,
    tpl_customer_active_noise,
    tpl_shipment_kpi_nested,
    tpl_payment_status_currency,
    tpl_multi_cte_cross_schema,
    tpl_dense_rank_lines,
    tpl_not_exists_unpaid,
    tpl_full_outer_chain,
    tpl_single_table_scan,
    tpl_cross_join_aggregate,
    tpl_hebrew_invoice_bridge,
    tpl_hebrew_multi_view_join,
    tpl_hebrew_payments_lines,
)


def _variant_params(base_idx: int, variant: int) -> tuple[int, int]:
    return base_idx * 31 + variant, variant


def _connection_string() -> str | None:
    cs = os.environ.get("MSSQL_CONNECTION_STRING", "").strip()
    return cs or None


def _validate_sql_batch(conn_str: str, sql: str) -> str | None:
    """Return error message if SQL fails, else None."""
    try:
        import pyodbc  # type: ignore
    except ImportError:
        return None

    conn = None
    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        cur.fetchall()
        return None
    except Exception as exc:
        return str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def generate_queries(
    count: int,
    *,
    seed: int = 42,
    validate: bool = True,
    conn_str: str | None = None,
) -> list[str]:
    if count < 1:
        raise ValueError("count must be >= 1")

    rng = random.Random(seed)
    seen: set[str] = set()
    out: list[str] = []
    rejected = 0
    idx = 0
    variant_per_template = 6
    cs = conn_str if validate else None
    if validate and cs is None:
        cs = _connection_string()
        if cs is None:
            raise RuntimeError(
                "Validation enabled but MSSQL_CONNECTION_STRING is not set. "
                "Run setup_dev_mssql.py first or pass --no-validate."
            )

    while len(out) < count:
        tpl = TEMPLATE_FNS[idx % len(TEMPLATE_FNS)]
        for variant in range(variant_per_template):
            sub_idx, _ = _variant_params(idx, variant)
            sub_rng = random.Random(seed + sub_idx)
            sql = tpl(sub_idx, sub_rng)
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            if cs is not None:
                err = _validate_sql_batch(cs, sql)
                if err is not None:
                    rejected += 1
                    continue
            seen.add(digest)
            out.append(sql)
            if len(out) >= count:
                break
        idx += 1
        if idx > count * 20:
            raise RuntimeError(
                f"Could not produce {count} valid queries (accepted={len(out)}, rejected={rejected})."
            )

    if cs is not None:
        print(f"Validated {len(out):,} queries against SQL Server ({rejected:,} rejected).")
    return out


def write_jsonl_file(queries: list[str], out_path: Path) -> None:
    """Write AMA-compatible JSONL (for ``ama-ingest run --sql-logs``, not Live Query Store)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for sql in queries:
            row = {"env": "prod", "dialect": "tsql", "sql": sql.strip()}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_sql_file(queries: list[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = textwrap.dedent(
        f"""\
        /*
          Kfar Supply — validated T-SQL benchmark ({len(queries):,} queries)
          Target DB: {TARGET_DB}
          Tables: dbo.customers, dbo.orders, dbo.order_lines,
                  finance.invoices, finance.payments, logistics.shipments

          English DDL columns only (matches setup_dev_mssql.py inferred schema).
          Generated by tools/generate_kfar_benchmark.py
        */
        USE {TARGET_DB};
        GO

        """
    )
    chunks: list[str] = [header]
    for i, sql in enumerate(queries, start=1):
        tagged = f"/* ama-bench-q{i:05d} */\n{sql.strip()}"
        chunks.append(f"-- Query {i}\n{tagged}\nGO\n")
    out_path.write_text("\n".join(chunks), encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate valid Kfar Supply T-SQL benchmark queries.")
    p.add_argument("--count", type=int, default=1000, help="Number of distinct queries (default: 1000)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output .sql path")
    p.add_argument(
        "--jsonl-out",
        type=Path,
        default=None,
        help="Optional AMA JSONL log path (file-based ingest; bypasses Query Store)",
    )
    p.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip live SQL Server validation (not recommended)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    args = _parse_args(argv)
    queries = generate_queries(
        args.count,
        seed=args.seed,
        validate=not args.no_validate,
    )
    write_sql_file(queries, args.out.resolve())
    print(f"Wrote {len(queries):,} queries -> {args.out.resolve()}")
    if args.jsonl_out is not None:
        write_jsonl_file(queries, args.jsonl_out.resolve())
        print(f"Wrote {len(queries):,} JSONL rows -> {args.jsonl_out.resolve()}")
    print(
        "Next: python tools/execute_kfar_benchmark.py  "
        "(then re-run Live connection with log end date = today)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
