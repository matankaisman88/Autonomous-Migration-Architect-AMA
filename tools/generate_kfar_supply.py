#!/usr/bin/env python3
"""
Regenerate the Kfar Supply Ltd. demo dataset under ``sample_data/kfar_supply/``.

Kfar Supply is a fictional Israeli B2B wholesale distributor migrating a 12-year-old
on-premise SQL Server estate to Azure Synapse. The SQL log mixes Hebrew business
column aliases with English DDL names to exercise glossary (high confidence),
exact DDL match, review-band typos, multi-schema discovery, lineage, and planner
ordering.

**Eight tables**

- ``dbo.orders`` — Hebrew: סכום, סטטוס, תאריך_יצירה, הנחה; DDL: order_id, customer_id;
  review: orderid, invoiceid, shipmentid, paymentid, customerid (dedicated review rows).
- ``dbo.order_lines`` — Hebrew: כמות, מחיר; DDL: line_id, order_id, product_id.
- ``dbo.customers`` — Hebrew: שם_לקוח, עיר, קוד_מדינה; DDL: customer_id, email;
  review: custname.
- ``finance.invoices`` — Hebrew: חשבונית, סכום_נטו, מעמ; DDL: invoice_id, order_id;
  review: invoiceamt, paymentstatus (dedicated review rows).
- ``finance.payments`` — Hebrew: תשלום, תאריך_תשלום; DDL: payment_id, invoice_id.
- ``logistics.shipments`` — Hebrew: מספר_מעקב, סטטוס_משלוח; DDL: shipment_id, order_id;
  review: trackingnum, warehouseid (dedicated review rows).
- ``legacy_hebrew.חשבוניות`` — legacy billing; Hebrew-only columns (no DDL manifest).
- ``temp_junk.Tmp_staging`` — technical-debt staging table (no DDL manifest).

**Merge confidence tiers exercised**

- Glossary path (~0.95): Hebrew terms resolved via ``kfar_glossary.json``.
- Exact/normalized DDL (~0.98): English DDL column names in SQL.
- Review band (~0.40–0.79): dedicated SQL rows with compound DDL names with underscore
  removed (``orderid``, ``invoiceid``, ``shipmentid``, ``paymentid``, ``customerid``)
  plus ``kfar_glossary_dirty.json`` overlays.

**Full demo pipeline**

From the repository root (after ``pip install -e .``), use one shell line for ``ama-ingest run``
(PowerShell does not treat ``\\`` as line continuation)::

    python tools/generate_kfar_supply.py
    ama-ingest run --data-root . --sql-logs "sample_data/kfar_supply/sql_logs/*.jsonl" --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json --glossary sample_data/kfar_supply/glossary/kfar_glossary.json --glossary-dirty sample_data/kfar_supply/glossary/kfar_glossary_dirty.json --comms-dir sample_data/kfar_supply/comms --git-sql-roots sample_data/kfar_supply/git_sql --target-schema dbo --target-table orders --discovery-mode --format json -o kfar_report.json
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KFAR = ROOT / "sample_data" / "kfar_supply"
BASE_GLOSSARY = ROOT / "sample_data" / "glossary" / "he_en_columns.json"

# orders↔invoices and invoices↔payments joins weighted higher for lineage + priority story
JOIN_WEIGHTS = [100, 120, 60, 60, 70]


def _proportion_split(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` across ``weights`` using largest-remainder (exact sum)."""
    s = sum(weights)
    exact = [total * w / s for w in weights]
    out = [int(x) for x in exact]
    rem = total - sum(out)
    order = sorted(
        range(len(weights)),
        key=lambda i: exact[i] - out[i],
        reverse=True,
    )
    for i in range(rem):
        out[order[i % len(order)]] += 1
    return out


def _dialect(rng: random.Random) -> str:
    """95% T-SQL, 5% Snowflake (dialect tag only)."""
    return "snowflake" if rng.random() < 0.05 else "tsql"


def _sql_orders_invoices(rng: random.Random) -> str:
    oid = rng.randint(1, 50000)
    return (
        f"SELECT o.order_id, o.[סכום], o.[סטטוס], i.[חשבונית], i.[סכום_נטו] "
        f"FROM dbo.orders o INNER JOIN finance.invoices i ON o.order_id = i.order_id "
        f"WHERE o.order_id = {oid}"
    )


def _sql_orders_lines(rng: random.Random) -> str:
    oid = rng.randint(1, 50000)
    return (
        f"SELECT ol.line_id, ol.[כמות], ol.[מחיר], o.[תאריך_יצירה] "
        f"FROM dbo.order_lines ol JOIN dbo.orders o ON ol.order_id = o.order_id "
        f"WHERE o.order_id = {oid}"
    )


def _sql_orders_shipments(rng: random.Random) -> str:
    oid = rng.randint(1, 50000)
    return (
        f"SELECT s.shipment_id, s.[מספר_מעקב], s.[סטטוס_משלוח], o.customer_id "
        f"FROM logistics.shipments s INNER JOIN dbo.orders o ON s.order_id = o.order_id "
        f"WHERE o.order_id = {oid}"
    )


def _sql_invoices_payments(rng: random.Random) -> str:
    iid = rng.randint(1, 40000)
    return (
        f"SELECT p.payment_id, p.[תשלום], p.[תאריך_תשלום], i.invoice_id "
        f"FROM finance.payments p JOIN finance.invoices i ON p.invoice_id = i.invoice_id "
        f"WHERE i.invoice_id = {iid}"
    )


def _sql_customers_orders(rng: random.Random) -> str:
    cid = rng.randint(1, 30000)
    return (
        f"SELECT c.customer_id, c.[שם_לקוח], c.[עיר], o.order_id, o.[הנחה] "
        f"FROM dbo.customers c INNER JOIN dbo.orders o ON c.customer_id = o.customer_id "
        f"WHERE c.customer_id = {cid}"
    )


def _sql_orders_bilingual_glossary_probe() -> str:
    """Hebrew + English DDL in the same SELECT for co-occurrence glossary mining."""
    return (
        "SELECT order_id, [סכום], amount, [סטטוס], status, [תאריך_יצירה], created_at "
        "FROM dbo.orders WHERE order_id > 0"
    )


def _sql_orderlines_bilingual_probe() -> str:
    """כמות->quantity, מחיר->unit_price adjacency pairs for co-occurrence mining."""
    return (
        "SELECT line_id, [כמות], quantity, [מחיר], unit_price "
        "FROM dbo.order_lines WHERE line_id > 0"
    )


def _sql_invoices_bilingual_probe() -> str:
    """מעמ->vat_rate, סכום_נטו->net_amount adjacency pairs."""
    return (
        "SELECT invoice_id, [מעמ], vat_rate, [סכום_נטו], net_amount "
        "FROM finance.invoices WHERE invoice_id > 0"
    )


def _sql_shipments_bilingual_probe() -> str:
    """מספר_מעקב->tracking_number, סטטוס_משלוח->shipment_status adjacency pairs."""
    return (
        "SELECT shipment_id, [מספר_מעקב], tracking_number, [סטטוס_משלוח], shipment_status "
        "FROM logistics.shipments WHERE shipment_id > 0"
    )


_BILINGUAL_PROBES = (
    _sql_orders_bilingual_glossary_probe,
    _sql_orderlines_bilingual_probe,
    _sql_invoices_bilingual_probe,
    _sql_shipments_bilingual_probe,
)


def _review_orderid() -> str:
    """orderid -> order_id (lx=0.933, blended≈0.41 -> review band)."""
    return (
        "SELECT orderid, customer_id, status, amount "
        "FROM dbo.orders WHERE orderid > 0"
    )


def _review_invoiceid() -> str:
    """invoiceid -> invoice_id (lx=0.947, blended≈0.42 -> review band)."""
    return (
        "SELECT invoiceid, order_id, amount, status "
        "FROM finance.invoices WHERE invoiceid IS NOT NULL"
    )


def _review_shipmentid() -> str:
    """shipmentid -> shipment_id (lx=0.952, blended≈0.42 -> review band)."""
    return (
        "SELECT shipmentid, order_id, tracking_number "
        "FROM logistics.shipments WHERE shipmentid > 0"
    )


def _review_paymentid() -> str:
    """paymentid -> payment_id (lx=0.947, blended≈0.42 -> review band)."""
    return (
        "SELECT paymentid, invoice_id, amount, paid_at "
        "FROM finance.payments WHERE paymentid IS NOT NULL"
    )


def _review_customerid() -> str:
    """customerid -> customer_id (lx=0.952, blended≈0.42 -> review band). Keep existing."""
    return (
        "SELECT customerid, order_id, status FROM dbo.orders "
        "WHERE customerid > 0 AND order_id IS NOT NULL"
    )


_REVIEW_POOL: list[tuple[object, int]] = [
    (_review_orderid, 22),
    (_review_invoiceid, 20),
    (_review_shipmentid, 20),
    (_review_paymentid, 20),
    (_review_customerid, 18),
]

# Single-table rotation: kind 3 (invoices) appears twice per 9 slots vs kind 4 (payments)
_SINGLE_KIND_CYCLE = (0, 1, 2, 3, 3, 4, 5, 6, 7)


def _single_sql_factory(kind: int, rng: random.Random) -> str:
    """Rotate single-table queries across inventory (deterministic variety)."""
    n = rng.randint(1, 90000)
    if kind % 8 == 0:
        return (
            f"SELECT order_id, customer_id, [סכום], [סטטוס], customerid "
            f"FROM dbo.orders WHERE order_id = {n}"
        )
    if kind % 8 == 1:
        return (
            f"SELECT line_id, order_id, product_id, [כמות], [מחיר] "
            f"FROM dbo.order_lines WHERE line_id = {n}"
        )
    if kind % 8 == 2:
        return (
            f"SELECT customer_id, [שם_לקוח], email, custname, [קוד_מדינה] "
            f"FROM dbo.customers WHERE customer_id = {n}"
        )
    if kind % 8 == 3:
        return (
            f"SELECT invoice_id, order_id, [חשבונית], [מעמ], [סכום_נטו] "
            f"FROM finance.invoices WHERE invoice_id = {n}"
        )
    if kind % 8 == 4:
        return (
            f"SELECT payment_id, invoice_id, [תשלום], [תאריך_תשלום] "
            f"FROM finance.payments WHERE payment_id = {n}"
        )
    if kind % 8 == 5:
        return (
            f"SELECT shipment_id, order_id, [מספר_מעקב], [סטטוס_משלוח] "
            f"FROM logistics.shipments WHERE shipment_id = {n}"
        )
    if kind % 8 == 6:
        return (
            f"SELECT [סכום], [חשבונית], [תאריך_יצירה], [שם_לקוח] "
            f"FROM legacy_hebrew.חשבוניות WHERE [חשבונית] = {n}"
        )
    return f"SELECT * FROM temp_junk.Tmp_staging WHERE id = {n}"


def _build_jsonl_lines(rng: random.Random, n_lines: int) -> list[dict[str, str]]:
    """Build ``n_lines`` JSONL row dicts (prod env)."""
    join_target = min(n_lines, max(0, round(n_lines * 380 / 15000)))
    splits = _proportion_split(join_target, JOIN_WEIGHTS) if join_target > 0 else [0, 0, 0, 0, 0]
    factories = [
        _sql_orders_invoices,
        _sql_orders_lines,
        _sql_orders_shipments,
        _sql_invoices_payments,
        _sql_customers_orders,
    ]
    rows: list[dict[str, str]] = []
    for count, factory in zip(splits, factories, strict=True):
        for _ in range(count):
            rows.append(
                {"env": "prod", "dialect": _dialect(rng), "sql": factory(rng)},
            )
    remaining = n_lines - len(rows)
    review_cap = max(0, round(n_lines * 0.02))
    bilingual_n = min(max(0, round(n_lines * 0.010)), remaining)
    review_target = min(review_cap, remaining - bilingual_n)
    review_funcs = [f for f, _w in _REVIEW_POOL]
    review_weights = [w for _f, w in _REVIEW_POOL]
    review_splits = _proportion_split(review_target, review_weights) if review_target > 0 else [0, 0, 0, 0, 0]
    for count, fn in zip(review_splits, review_funcs, strict=True):
        for _ in range(count):
            sql = fn()
            rows.append(
                {"env": "prod", "dialect": _dialect(rng), "sql": sql},
            )
    for idx in range(bilingual_n):
        probe_fn = _BILINGUAL_PROBES[idx % len(_BILINGUAL_PROBES)]
        rows.append(
            {
                "env": "prod",
                "dialect": _dialect(rng),
                "sql": probe_fn(),
            },
        )
    single_lines = n_lines - len(rows)
    for i in range(single_lines):
        kind = _SINGLE_KIND_CYCLE[i % len(_SINGLE_KIND_CYCLE)]
        rows.append(
            {
                "env": "prod",
                "dialect": _dialect(rng),
                "sql": _single_sql_factory(kind, rng),
            },
        )
    return rows


def _write_ddl_files(ddl_dir: Path) -> None:
    """Write per-table column JSON files."""
    specs: dict[str, list[str]] = {
        "dbo_orders.json": [
            "order_id",
            "customer_id",
            "status",
            "amount",
            "created_at",
            "discount",
            "currency",
            "sales_rep_id",
        ],
        "dbo_order_lines.json": [
            "line_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount",
            "net_amount",
        ],
        "dbo_customers.json": [
            "customer_id",
            "customer_name",
            "email",
            "city",
            "country_code",
            "phone",
            "is_active",
            "created_at",
        ],
        "finance_invoices.json": [
            "invoice_id",
            "order_id",
            "amount",
            "net_amount",
            "vat_amount",
            "vat_rate",
            "status",
            "due_date",
            "created_at",
        ],
        "finance_payments.json": [
            "payment_id",
            "invoice_id",
            "amount",
            "paid_at",
            "payment_status",
            "currency",
        ],
        "logistics_shipments.json": [
            "shipment_id",
            "order_id",
            "tracking_number",
            "shipment_status",
            "shipped_at",
            "warehouse_id",
        ],
    }
    for name, cols in specs.items():
        p = ddl_dir / name
        p.write_text(
            json.dumps({"columns": cols}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _write_manifest(ddl_dir: Path) -> None:
    """Write ``kfar_manifest.json`` with paths relative to repo root."""
    manifest = {
        "_comment": "Kfar Supply DDL manifest — schema.table -> DDL file relative to data root",
        "dbo.orders": "sample_data/kfar_supply/ddl/dbo_orders.json",
        "dbo.order_lines": "sample_data/kfar_supply/ddl/dbo_order_lines.json",
        "dbo.customers": "sample_data/kfar_supply/ddl/dbo_customers.json",
        "finance.invoices": "sample_data/kfar_supply/ddl/finance_invoices.json",
        "finance.payments": "sample_data/kfar_supply/ddl/finance_payments.json",
        "logistics.shipments": "sample_data/kfar_supply/ddl/logistics_shipments.json",
    }
    (ddl_dir / "kfar_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_glossaries(gloss_dir: Path) -> None:
    """Primary glossary from shared Hebrew file; dirty file holds shorthand overlays."""
    base = json.loads(BASE_GLOSSARY.read_text(encoding="utf-8"))
    (gloss_dir / "kfar_glossary.json").write_text(
        json.dumps(base, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    dirty = {
        "סטט": "status",
        "תאריך": "created_at",
        "סכ": "amount",
        "כמ": "quantity",
        "מחיר_מכירה": "unit_price",
        "invdate": "created_at",
        "ordstatus": "status",
        "custid": "customer_id",
        "payref": "payment_id",
        "invnum": "invoice_id",
    }
    (gloss_dir / "kfar_glossary_dirty.json").write_text(
        json.dumps(dirty, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_git_sql(git_root: Path) -> None:
    """Check in representative Git-resident SQL."""
    monthly = git_root / "reports" / "monthly_revenue.sql"
    monthly.parent.mkdir(parents=True, exist_ok=True)
    monthly.write_text(
        "-- Kfar Supply: revenue roll-up (orders + invoices)\n"
        "SELECT o.order_id, o.amount AS order_amt, i.net_amount\n"
        "FROM dbo.orders o\n"
        "JOIN finance.invoices i ON o.order_id = i.order_id\n"
        "WHERE o.status <> N'cancelled';\n",
        encoding="utf-8",
    )
    cust = git_root / "reports" / "customer_summary.sql"
    cust.write_text(
        "-- Customer activity\n"
        "SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS cnt\n"
        "FROM dbo.customers c\n"
        "LEFT JOIN dbo.orders o ON c.customer_id = o.customer_id\n"
        "GROUP BY c.customer_id, c.customer_name;\n",
        encoding="utf-8",
    )
    ship = git_root / "reports" / "shipment_kpi.sql"
    ship.write_text(
        "-- Shipment KPIs vs orders\n"
        "SELECT s.shipment_id, s.tracking_number, o.order_id\n"
        "FROM logistics.shipments s\n"
        "INNER JOIN dbo.orders o ON s.order_id = o.order_id;\n",
        encoding="utf-8",
    )
    legacy = git_root / "legacy" / "hebrew_invoice_bridge.sql"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "-- Bridge legacy Hebrew billing to finance.invoices\n"
        "SELECT h.[חשבונית], h.[סכום], i.invoice_id\n"
        "FROM legacy_hebrew.חשבוניות h\n"
        "LEFT JOIN finance.invoices i ON h.[חשבונית] = i.invoice_id;\n",
        encoding="utf-8",
    )


def _write_comms(comms_dir: Path) -> None:
    """Slack-style JSONL (fixed timestamps — no wall clock in content)."""
    comms_dir.mkdir(parents=True, exist_ok=True)
    msgs: list[tuple[str, str, str]] = [
        (
            "migration-planning",
            "1704067200.000101",
            "Cutover plan: dbo.orders must land before finance.invoices — finance depends on order_id.",
        ),
        (
            "migration-planning",
            "1704067200.000102",
            "Confirm lineage from dbo.orders to dbo.order_lines for wave 1.",
        ),
        (
            "finance-it",
            "1704067200.000103",
            "finance.invoices and finance.payments: validate paydt mapping in review queue.",
        ),
        (
            "finance-it",
            "1704067200.000104",
            "Hebrew חשבונית column still appears in legacy_hebrew.חשבוניות — needs manual map.",
        ),
        (
            "exec-reporting",
            "1704067200.000105",
            "VP asks: timeline for Finance domain once invoices and payments are in Synapse.",
        ),
        (
            "migration-planning",
            "1704067200.000106",
            "Is temp_junk.Tmp_staging safe to drop post-migration? No DDL entry.",
        ),
        (
            "finance-it",
            "1704067200.000107",
            "logistics.shipments joins dbo.orders on order_id — include in discovery inventory.",
        ),
        (
            "migration-planning",
            "1704067200.000108",
            "Glossary should resolve סכום and סטטוס on dbo.orders to amount/status.",
        ),
        (
            "exec-reporting",
            "1704067200.000109",
            "Kfar Supply: prioritize wholesale B2B orders table dbo.orders as migration anchor.",
        ),
        (
            "finance-it",
            "1704067200.000110",
            "Review customerid vs customer_id on orders — HITL candidate.",
        ),
        (
            "migration-planning",
            "1704067200.000111",
            "Domain clustering: Operations (orders, lines), Finance (invoices, payments), Logistics.",
        ),
        (
            "finance-it",
            "1704067200.000112",
            "Payments after invoices in planner topo-sort — confirm in report.",
        ),
        (
            "migration-planning",
            "1704067200.000113",
            "dbo.customers.custname flagged — dirty glossary should map to customer_name.",
        ),
        (
            "exec-reporting",
            "1704067200.000114",
            "Azure Synapse target: Hebrew identifiers in legacy_hebrew.חשבוניות remain risky.",
        ),
        (
            "finance-it",
            "1704067200.000115",
            "finance.payments: תאריך_תשלום → paid_at via glossary.",
        ),
        (
            "migration-planning",
            "1704067200.000116",
            "Cross-schema JOIN finance.invoices to dbo.orders appears in monthly revenue SQL.",
        ),
        (
            "migration-planning",
            "1704067200.000117",
            "Tracknum on shipments vs tracking_number in DDL — review band.",
        ),
        (
            "finance-it",
            "1704067200.000118",
            "Invoices invamt column in logs — near net_amount.",
        ),
        (
            "exec-reporting",
            "1704067200.000119",
            "Operations wave: dbo.order_lines qty/prc shorthand needs QA.",
        ),
        (
            "migration-planning",
            "1704067200.000120",
            "Six mapped DDL tables + legacy_hebrew.חשבוניות + temp_junk.Tmp_staging in scope.",
        ),
        (
            "finance-it",
            "1704067200.000121",
            "Synapse pipeline: load dbo.orders first, then finance.invoices.",
        ),
        (
            "migration-planning",
            "1704067200.000122",
            "Hebrew invoice bridge SQL references legacy_hebrew.חשבוניות LEFT JOIN finance.invoices.",
        ),
        (
            "exec-reporting",
            "1704067200.000123",
            "Risk: Tmp_staging in temp_junk used for ad-hoc loads.",
        ),
        (
            "finance-it",
            "1704067200.000124",
            "מעמ and סכום_נטו on finance.invoices map to vat_rate and net_amount.",
        ),
        (
            "migration-planning",
            "1704067200.000125",
            "Discovery should list logistics.shipments with logistics domain.",
        ),
        (
            "finance-it",
            "1704067200.000126",
            "Payment תשלום field ambiguity — watch merge confidence.",
        ),
        (
            "migration-planning",
            "1704067200.000127",
            "Git SQL under sample_data/kfar_supply/git_sql mirrors analyst queries.",
        ),
        (
            "exec-reporting",
            "1704067200.000128",
            "Board readout: migration waves from AMA planner JSON.",
        ),
        (
            "finance-it",
            "1704067200.000129",
            "orddate typo columns on dbo.orders — review queue expectation.",
        ),
        (
            "migration-planning",
            "1704067200.000130",
            "Kfar Supply קפאר סאפליי: bilingual columns documented in glossary.",
        ),
        (
            "finance-it",
            "1704067200.000131",
            "finance.invoices joined to finance.payments on invoice_id in SQL logs.",
        ),
        (
            "migration-planning",
            "1704067200.000132",
            "dbo.customers joined to dbo.orders for customer_summary.sql pattern.",
        ),
        (
            "exec-reporting",
            "1704067200.000133",
            "Impact vs readiness scatter should highlight dbo.orders hub.",
        ),
        (
            "finance-it",
            "1704067200.000134",
            "Legacy חשבוניות table still in production per ops thread.",
        ),
        (
            "migration-planning",
            "1704067200.000135",
            "Use discovery-mode run for full inventory including unmapped tables.",
        ),
        (
            "finance-it",
            "1704067200.000136",
            "Shipments KPI SQL joins logistics.shipments to dbo.orders.",
        ),
        (
            "exec-reporting",
            "1704067200.000137",
            "Finance domain cluster: invoices before payments ordering.",
        ),
        (
            "migration-planning",
            "1704067200.000138",
            "HITL: expect several 0.4–0.8 confidence merges from dirty shorthand.",
        ),
        (
            "finance-it",
            "1704067200.000139",
            "Validate מספר_מעקב maps to tracking_number on shipments.",
        ),
        (
            "migration-planning",
            "1704067200.000140",
            "temp_junk.Tmp_staging — confirm no production dependency before drop.",
        ),
    ]
    path = comms_dir / "kfar_slack.jsonl"
    lines = [
        json.dumps(
            {"channel": ch, "ts": ts, "text": tx},
            ensure_ascii=False,
            sort_keys=True,
        )
        for ch, ts, tx in msgs
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_all(lines: int) -> None:
    """Write every Kfar Supply artifact under ``sample_data/kfar_supply/``."""
    rng = random.Random(7)
    sql_dir = KFAR / "sql_logs"
    ddl_dir = KFAR / "ddl"
    gloss_dir = KFAR / "glossary"
    comms_dir = KFAR / "comms"
    git_root = KFAR / "git_sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    ddl_dir.mkdir(parents=True, exist_ok=True)
    gloss_dir.mkdir(parents=True, exist_ok=True)

    rows = _build_jsonl_lines(rng, lines)
    jsonl_path = sql_dir / "kfar_prod.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )

    _write_ddl_files(ddl_dir)
    _write_manifest(ddl_dir)
    _write_glossaries(gloss_dir)
    _write_comms(comms_dir)
    _write_git_sql(git_root)


def main() -> None:
    """CLI entrypoint."""
    ap = argparse.ArgumentParser(description="Generate Kfar Supply demo dataset.")
    ap.add_argument(
        "--lines",
        type=int,
        default=15000,
        help="Number of SQL log JSONL rows (default: 15000)",
    )
    args = ap.parse_args()
    generate_all(max(1, args.lines))
    print(f"Wrote Kfar Supply dataset under {KFAR}")


if __name__ == "__main__":
    main()
