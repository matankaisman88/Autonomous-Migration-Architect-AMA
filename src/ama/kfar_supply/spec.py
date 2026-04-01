"""Kfar Supply core table definitions (six mapped DDL tables)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KfarTable:
    """One logical inventory table for demo DDL + manifest."""

    schema_name: str
    table_name: str
    ddl_json_filename: str
    columns: tuple[str, ...]
    primary_key: str

    @property
    def full_key(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


KFAR_TABLES: tuple[KfarTable, ...] = (
    KfarTable(
        "dbo",
        "customers",
        "dbo_customers.json",
        (
            "customer_id",
            "customer_name",
            "email",
            "city",
            "country_code",
            "phone",
            "is_active",
            "created_at",
        ),
        "customer_id",
    ),
    KfarTable(
        "dbo",
        "orders",
        "dbo_orders.json",
        (
            "order_id",
            "customer_id",
            "status",
            "amount",
            "created_at",
            "discount",
            "currency",
            "sales_rep_id",
        ),
        "order_id",
    ),
    KfarTable(
        "dbo",
        "order_lines",
        "dbo_order_lines.json",
        ("line_id", "order_id", "product_id", "quantity", "unit_price", "discount", "net_amount"),
        "line_id",
    ),
    KfarTable(
        "finance",
        "invoices",
        "finance_invoices.json",
        (
            "invoice_id",
            "order_id",
            "amount",
            "net_amount",
            "vat_amount",
            "vat_rate",
            "status",
            "due_date",
            "created_at",
        ),
        "invoice_id",
    ),
    KfarTable(
        "finance",
        "payments",
        "finance_payments.json",
        ("payment_id", "invoice_id", "amount", "paid_at", "payment_status", "currency"),
        "payment_id",
    ),
    KfarTable(
        "logistics",
        "shipments",
        "logistics_shipments.json",
        ("shipment_id", "order_id", "tracking_number", "shipment_status", "shipped_at", "warehouse_id"),
        "shipment_id",
    ),
)


def expected_column_sets() -> dict[str, frozenset[str]]:
    return {t.full_key: frozenset(c.lower() for c in t.columns) for t in KFAR_TABLES}
