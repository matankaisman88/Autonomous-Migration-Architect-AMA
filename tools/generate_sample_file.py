"""
Build the canonical demo SQL log: ``sample_data/sql_logs/sample_file.jsonl``.

Designed for default AMA settings (``sales.orders`` + DDL ``orders_columns.json`` +
glossaries ``he_en_columns.json`` + ``he_en_columns_dirty.json`` (merged; dirty adds shorthand/typos)):

- Large-scale row volume (default 12k lines; override with ``--lines``).
- **Multiple business domains** via schema + table names (``_heuristic_domain``): ``sales.orders``
  → Operations (not Finance); ``finance.*``, ``logistics.*``, ``crm.*``, ``marketing.*``,
  ``analytics.*``; Legacy Core / Technical Debt edge cases.
- ``sales.orders`` column naming tuned for **varied merge confidence** in the Business Glossary:
  **0.98** exact DDL on ``order_id`` and ``customer_id``; **0.95** glossary Hebrew for
  ``status`` / ``amount`` / ``created_at`` / ``discount`` / ``unit_price`` / ``quantity`` / ``vat_amount``
  (e.g. ``סטטוס``, ``סכום``, ``תאריך_יצירה``, ``הנחה``, ``מחיר``, ``כמות``, ``סכום_מעמ`` —
  do not also emit English duplicates on the same query line for those Hebrew columns).
- **Review band** (~0.39–0.45): ``customerid``, ``orderid``, ``created`` (vector_lexical).
- Cross-schema JOINs for discovery, lineage, and domain matrix in the dashboard.

Regenerate: ``python tools/generate_sample_file.py`` or ``python tools/generate_sample_file.py --lines 20000``
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sample_data" / "sql_logs" / "sample_file.jsonl"

RNG = random.Random(42)
DIALECTS = ("postgres", "tsql", "mysql", "snowflake")


def _row(dialect: str, sql: str, env: str = "prod") -> str:
    return json.dumps({"env": env, "dialect": dialect, "sql": sql}, ensure_ascii=False)


# --- Target table (DDL scope) — merge confidence tiers (see module docstring) ---

def _single_orders_glossary_heavy() -> str:
    """English ids (0.98) + Hebrew for status/amount/date (0.95)."""
    return (
        "SELECT order_id, customer_id, סטטוס, סכום, תאריך_יצירה FROM sales.orders "
        "WHERE סטטוס IN (N'open', N'paid') AND סכום > 0"
    )


def _single_orders_glossary_alt() -> str:
    return (
        "SELECT order_id, customer_id, סטטוס, סכום FROM sales.orders "
        "WHERE תאריך_יצירה >= '2024-01-01' AND סטטוס <> N'cancelled'"
    )


def _single_orders_partial_he() -> str:
    return (
        "SELECT order_id, customer_id, סכום FROM sales.orders o "
        "WHERE o.תאריך_יצירה >= '2024-01-01'"
    )


def _single_orders_extended_pricing() -> str:
    """Hebrew for discount, unit_price, quantity, vat_amount (glossary → extended DDL columns)."""
    return (
        "SELECT order_id, מחיר, כמות, הנחה, סכום_מעמ FROM sales.orders "
        "WHERE כמות > 0 AND סכום_מעמ >= 0"
    )


def _single_orders_mixed_pricing() -> str:
    """Mix core + extended Hebrew columns on ``sales.orders``."""
    return (
        "SELECT order_id, סטטוס, סכום, מחיר, כמות, תאריך_יצירה FROM sales.orders "
        "WHERE הנחה > 0 OR דיסקונט IS NOT NULL"
    )


def _single_orders_dirty_glossary() -> str:
    """Latin shorthand / typos resolved via ``he_en_columns_dirty.json`` (qty, disc, u_price, vat_amt)."""
    return (
        "SELECT order_id, cust_id, qty, disc, u_price, vat_amt, crtd_at FROM sales.orders "
        "WHERE qty > 0 AND vat_amt >= 0"
    )


def _orders_review_typos() -> str:
    """Ambiguous legacy names → vector_lexical in review band (below 0.8 confirmed)."""
    return (
        "SELECT customerid, orderid, created FROM sales.orders "
        "WHERE customerid > 0 AND orderid < 100000"
    )


def _orders_review_typos_b() -> str:
    return "SELECT customerid, created FROM sales.orders WHERE orderid IS NOT NULL"


# --- Finance domain (invoice, credit, commission, budget, tax, contract, payment) ---

def _single_finance_invoices() -> str:
    return (
        "SELECT id, order_id, total, status, due_date FROM finance.invoices WHERE status = 'posted'"
    )


def _single_finance_payments() -> str:
    return "SELECT id, invoice_id, amount, paid_at FROM finance.payments WHERE amount > 0"


def _single_finance_credits() -> str:
    return "SELECT id, customer_id, amount FROM finance.credits WHERE status = 'open'"


def _single_finance_commissions() -> str:
    return "SELECT id, rep_id, order_id, amount FROM finance.commissions WHERE period = 'Q1'"


def _single_finance_budget() -> str:
    return "SELECT id, department, amount FROM finance.budget_lines WHERE fiscal_year = 2025"


def _single_finance_tax() -> str:
    return "SELECT id, region, rate FROM finance.tax_rates WHERE active = 1"


def _single_finance_contracts() -> str:
    return "SELECT id, customer_id, start_date FROM finance.contracts WHERE status = 'active'"


# --- Logistics (ship, warehouse, inventory, product, stock, fulfill) ---

def _single_logistics_shipments() -> str:
    return "SELECT id, order_id, tracking_no FROM logistics.shipments WHERE status = 'in_transit'"


def _single_logistics_warehouses() -> str:
    return "SELECT id, name, region FROM logistics.warehouses WHERE active = 1"


def _single_logistics_inventory() -> str:
    return "SELECT id, sku, qty FROM logistics.inventory_snapshots WHERE qty > 0"


def _single_sales_products() -> str:
    return "SELECT id, sku, name FROM sales.products WHERE active = 1"


# --- CRM (customer, account, rep, territory, lead) ---

def _single_sales_customers() -> str:
    return "SELECT id, name, region, tier FROM sales.customers WHERE tier = 'gold'"


def _single_crm_accounts() -> str:
    return "SELECT id, name, segment FROM crm.accounts WHERE segment = 'enterprise'"


def _single_crm_reps() -> str:
    return "SELECT id, name, quota FROM crm.sales_reps WHERE region = 'EMEA'"


def _single_crm_territories() -> str:
    return "SELECT id, name FROM crm.territories WHERE active = 1"


def _single_crm_leads() -> str:
    return "SELECT id, name, stage FROM crm.leads WHERE stage = 'qualified'"


# --- Marketing (promo, campaign) ---

def _single_mkt_promotions() -> str:
    return "SELECT id, name, discount_pct FROM marketing.promotions WHERE active = 1"


def _single_mkt_campaigns() -> str:
    return "SELECT id, name, spend FROM marketing.campaign_results WHERE channel = 'email'"


# --- Analytics (forecast, target, return) ---

def _single_analytics_forecast() -> str:
    return "SELECT id, period, value FROM analytics.forecast_runs WHERE scenario = 'base'"


def _single_analytics_targets() -> str:
    return "SELECT id, team, goal FROM analytics.targets WHERE quarter = 1"


def _single_analytics_returns() -> str:
    return "SELECT id, order_id, reason FROM analytics.return_authorizations WHERE status = 'pending'"


# --- Legacy Core (Hebrew identifier, no ASCII letters in table name) ---

def _single_legacy_hebrew() -> str:
    return "SELECT id, DATA, NAME FROM legacy_hebrew.חשבוניות WHERE STATUS = 'open'"


def _join_legacy_orders() -> str:
    return (
        "SELECT o.order_id, h.DATA "
        "FROM sales.orders o "
        "INNER JOIN legacy_hebrew.חשבוניות h ON o.order_id = h.id "
        "WHERE o.סטטוס = N'paid'"
    )


# --- Technical Debt (TEMP / Tmp) ---

def _single_temp_junk() -> str:
    return "SELECT id, payload FROM temp_junk.Tmp_7 WHERE id > 0"


def _join_temp_orders() -> str:
    return (
        "SELECT o.order_id, t.payload "
        "FROM sales.orders o "
        "LEFT JOIN temp_junk.Tmp_7 t ON t.id = o.order_id "
        "WHERE o.סטטוס = N'open'"
    )


# --- Cross-domain JOINs (lineage + domains) ---

def _join_orders_customers() -> str:
    return (
        "SELECT o.order_id, o.סטטוס, c.name, c.region "
        "FROM sales.orders o "
        "INNER JOIN sales.customers c ON o.customer_id = c.id "
        "WHERE o.סטטוס = N'open'"
    )


def _join_orders_invoices() -> str:
    return (
        "SELECT o.order_id, o.סכום, i.total, i.due_date "
        "FROM sales.orders o "
        "INNER JOIN finance.invoices i ON i.order_id = o.order_id "
        "WHERE i.status = 'posted'"
    )


def _join_three_products() -> str:
    return (
        "SELECT o.order_id, ol.line_no, p.sku, ol.qty "
        "FROM sales.orders o "
        "INNER JOIN sales.order_lines ol ON o.order_id = ol.order_id "
        "INNER JOIN sales.products p ON ol.product_id = p.id "
        "WHERE o.סטטוס = N'shipped'"
    )


def _join_orders_customers_invoices() -> str:
    return (
        "SELECT o.order_id, c.name, i.total "
        "FROM sales.orders o "
        "JOIN sales.customers c ON o.customer_id = c.id "
        "JOIN finance.invoices i ON i.order_id = o.order_id"
    )


def _join_orders_shipments() -> str:
    return (
        "SELECT o.order_id, s.tracking_no, s.status "
        "FROM sales.orders o "
        "INNER JOIN logistics.shipments s ON s.order_id = o.order_id "
        "WHERE s.status = 'shipped'"
    )


def _join_invoices_payments() -> str:
    return (
        "SELECT i.id, p.amount, p.paid_at "
        "FROM finance.invoices i "
        "INNER JOIN finance.payments p ON p.invoice_id = i.id "
        "WHERE i.status = 'posted'"
    )


def _join_commissions_reps() -> str:
    return (
        "SELECT c.amount, r.name "
        "FROM finance.commissions c "
        "INNER JOIN crm.sales_reps r ON c.rep_id = r.id "
        "WHERE c.period = 'Q1'"
    )


def _join_orders_leads() -> str:
    return (
        "SELECT o.order_id, l.stage "
        "FROM sales.orders o "
        "INNER JOIN crm.leads l ON l.account_id = o.customer_id "
        "WHERE l.stage = 'qualified'"
    )


def _join_promotions_orderlines() -> str:
    return (
        "SELECT ol.order_id, pr.discount_pct "
        "FROM sales.order_lines ol "
        "INNER JOIN marketing.promotions pr ON ol.promo_id = pr.id "
        "WHERE pr.active = 1"
    )


def _join_forecast_budget() -> str:
    return (
        "SELECT f.period, b.amount "
        "FROM analytics.forecast_runs f "
        "INNER JOIN finance.budget_lines b ON f.period_id = b.id "
        "WHERE f.scenario = 'base'"
    )


def _join_returns_orders() -> str:
    return (
        "SELECT r.id, o.סטטוס "
        "FROM analytics.return_authorizations r "
        "INNER JOIN sales.orders o ON r.order_id = o.order_id "
        "WHERE r.status = 'pending'"
    )


def _union_orders_customers() -> str:
    return (
        "SELECT order_id, סטטוס FROM sales.orders WHERE order_id > 100 "
        "UNION ALL "
        "SELECT id AS order_id, 'n/a' AS col FROM sales.customers WHERE id < 50"
    )


def generate_lines(n: int) -> list[str]:
    lines: list[str] = []

    # sales.orders: Hebrew tokens for status/amount/date (0.95); English ids (0.98)
    orders_singles = [
        (_single_orders_glossary_heavy, 0.33),
        (_single_orders_glossary_alt, 0.20),
        (_single_orders_partial_he, 0.14),
        (_single_orders_extended_pricing, 0.14),
        (_single_orders_mixed_pricing, 0.09),
        (_single_orders_dirty_glossary, 0.10),
    ]
    orders_review = [
        (_orders_review_typos, 0.55),
        (_orders_review_typos_b, 0.45),
    ]
    finance_singles = [
        (_single_finance_invoices, 0.06),
        (_single_finance_payments, 0.04),
        (_single_finance_credits, 0.03),
        (_single_finance_commissions, 0.03),
        (_single_finance_budget, 0.03),
        (_single_finance_tax, 0.03),
        (_single_finance_contracts, 0.03),
    ]
    logistics_singles = [
        (_single_logistics_shipments, 0.04),
        (_single_logistics_warehouses, 0.03),
        (_single_logistics_inventory, 0.03),
        (_single_sales_products, 0.04),
    ]
    crm_singles = [
        (_single_sales_customers, 0.04),
        (_single_crm_accounts, 0.03),
        (_single_crm_reps, 0.03),
        (_single_crm_territories, 0.02),
        (_single_crm_leads, 0.03),
    ]
    mkt_analytics = [
        (_single_mkt_promotions, 0.02),
        (_single_mkt_campaigns, 0.02),
        (_single_analytics_forecast, 0.02),
        (_single_analytics_targets, 0.02),
        (_single_analytics_returns, 0.02),
    ]
    edge_singles = [
        (_single_legacy_hebrew, 0.02),
        (_single_temp_junk, 0.02),
    ]

    joins = [
        # sales / finance / logistics / crm / marketing / analytics
        (_join_orders_customers, 0.08),
        (_join_orders_invoices, 0.07),
        (_join_three_products, 0.07),
        (_join_orders_customers_invoices, 0.05),
        (_join_orders_shipments, 0.05),
        (_join_invoices_payments, 0.04),
        (_join_commissions_reps, 0.04),
        (_join_orders_leads, 0.04),
        (_join_promotions_orderlines, 0.03),
        (_join_forecast_budget, 0.03),
        (_join_returns_orders, 0.03),
        (_union_orders_customers, 0.03),
        (_join_legacy_orders, 0.02),
        (_join_temp_orders, 0.02),
    ]

    def _normalize(wlist: list[tuple]) -> list[tuple]:
        s = sum(w for _, w in wlist)
        return [(c, w / s) for c, w in wlist]

    def _pick(pool: list[tuple]) -> str:
        pool = _normalize(pool)
        r = RNG.random()
        acc = 0.0
        for fn, w in pool:
            acc += w
            if r <= acc:
                return fn()
        return pool[-1][0]()

    # Precompute strata for reproducible mix
    singles_pool: list[tuple] = [
        *orders_singles,
        *finance_singles,
        *logistics_singles,
        *crm_singles,
        *mkt_analytics,
        *edge_singles,
    ]

    for i in range(n):
        d = RNG.choice(DIALECTS)
        r = RNG.random()
        if r < 0.08:
            # Low-confidence / review-band legacy identifiers on sales.orders
            lines.append(_row(d, _pick(orders_review)))
        elif r < 0.36:
            # Primary: sales.orders (glossary vs exact DDL — see module docstring)
            lines.append(_row(d, _pick(orders_singles)))
        elif r < 0.80:
            lines.append(_row(d, _pick(joins)))
        else:
            # Domain diversity: finance / logistics / CRM / marketing / analytics singles
            lines.append(_row(d, _pick(singles_pool)))

    return lines


def main() -> None:
    p = argparse.ArgumentParser(description="Generate sample_data/sql_logs/sample_file.jsonl")
    p.add_argument(
        "--lines",
        type=int,
        default=12_000,
        help="Number of JSONL rows (default: 12000)",
    )
    args = p.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = generate_lines(max(100, args.lines))
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} lines to {OUT}")


if __name__ == "__main__":
    main()
