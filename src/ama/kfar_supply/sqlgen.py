"""Dialect-specific idempotent DDL and seed DML for Kfar Supply demo tables."""

from __future__ import annotations

import textwrap


def _tsql_schemas_and_tables() -> list[str]:
    return [
        textwrap.dedent(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'finance')
                EXEC(N'CREATE SCHEMA finance');
            """
        ).strip(),
        textwrap.dedent(
            """
            IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'logistics')
                EXEC(N'CREATE SCHEMA logistics');
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'dbo.customers', N'U') IS NULL
            CREATE TABLE dbo.customers (
              customer_id INT NOT NULL PRIMARY KEY,
              customer_name NVARCHAR(400) NULL,
              email NVARCHAR(320) NULL,
              city NVARCHAR(200) NULL,
              country_code NVARCHAR(16) NULL,
              phone NVARCHAR(64) NULL,
              is_active BIT NULL,
              created_at DATETIME2(3) NULL
            );
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'dbo.orders', N'U') IS NULL
            CREATE TABLE dbo.orders (
              order_id INT NOT NULL PRIMARY KEY,
              customer_id INT NULL,
              status NVARCHAR(64) NULL,
              amount DECIMAL(18,4) NULL,
              created_at DATETIME2(3) NULL,
              discount DECIMAL(18,4) NULL,
              currency NVARCHAR(8) NULL,
              sales_rep_id INT NULL
            );
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'dbo.order_lines', N'U') IS NULL
            CREATE TABLE dbo.order_lines (
              line_id INT NOT NULL PRIMARY KEY,
              order_id INT NULL,
              product_id INT NULL,
              quantity DECIMAL(18,4) NULL,
              unit_price DECIMAL(18,4) NULL,
              discount DECIMAL(18,4) NULL,
              net_amount DECIMAL(18,4) NULL
            );
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'finance.invoices', N'U') IS NULL
            CREATE TABLE finance.invoices (
              invoice_id INT NOT NULL PRIMARY KEY,
              order_id INT NULL,
              amount DECIMAL(18,4) NULL,
              net_amount DECIMAL(18,4) NULL,
              vat_amount DECIMAL(18,4) NULL,
              vat_rate DECIMAL(9,4) NULL,
              status NVARCHAR(64) NULL,
              due_date DATETIME2(3) NULL,
              created_at DATETIME2(3) NULL
            );
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'finance.payments', N'U') IS NULL
            CREATE TABLE finance.payments (
              payment_id INT NOT NULL PRIMARY KEY,
              invoice_id INT NULL,
              amount DECIMAL(18,4) NULL,
              paid_at DATETIME2(3) NULL,
              payment_status NVARCHAR(64) NULL,
              currency NVARCHAR(8) NULL
            );
            """
        ).strip(),
        textwrap.dedent(
            """
            IF OBJECT_ID(N'logistics.shipments', N'U') IS NULL
            CREATE TABLE logistics.shipments (
              shipment_id INT NOT NULL PRIMARY KEY,
              order_id INT NULL,
              tracking_number NVARCHAR(128) NULL,
              shipment_status NVARCHAR(64) NULL,
              shipped_at DATETIME2(3) NULL,
              warehouse_id INT NULL
            );
            """
        ).strip(),
    ]


def _tsql_data_reset() -> list[str]:
    return [
        "DELETE FROM finance.payments;",
        "DELETE FROM logistics.shipments;",
        "DELETE FROM finance.invoices;",
        "DELETE FROM dbo.order_lines;",
        "DELETE FROM dbo.orders;",
        "DELETE FROM dbo.customers;",
    ]


def _tsql_inserts() -> list[str]:
    return [
        textwrap.dedent(
            """
            INSERT INTO dbo.customers (customer_id, customer_name, email, city, country_code, phone, is_active, created_at)
            VALUES (1, N'Acme Wholesale', N'orders@acme.example', N'Tel Aviv', N'IL', N'+972-3-0000000', 1, SYSUTCDATETIME());
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO dbo.orders (order_id, customer_id, status, amount, created_at, discount, currency, sales_rep_id)
            VALUES (1, 1, N'open', 250.0000, SYSUTCDATETIME(), 0.0000, N'ILS', 10);
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO dbo.order_lines (line_id, order_id, product_id, quantity, unit_price, discount, net_amount)
            VALUES (1, 1, 1001, 2.0000, 125.0000, 0.0000, 250.0000);
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO finance.invoices (invoice_id, order_id, amount, net_amount, vat_amount, vat_rate, status, due_date, created_at)
            VALUES (1, 1, 250.0000, 210.0000, 40.0000, 0.17, N'open', SYSUTCDATETIME(), SYSUTCDATETIME());
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO finance.payments (payment_id, invoice_id, amount, paid_at, payment_status, currency)
            VALUES (1, 1, 210.0000, SYSUTCDATETIME(), N'posted', N'ILS');
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO logistics.shipments (shipment_id, order_id, tracking_number, shipment_status, shipped_at, warehouse_id)
            VALUES (1, 1, N'TRK-001', N'in_transit', SYSUTCDATETIME(), 7);
            """
        ).strip(),
    ]


def _ora_wrap_create(ddl: str) -> str:
    return textwrap.dedent(
        f"""
        BEGIN
          EXECUTE IMMEDIATE q'[
        {ddl.strip()}
        ]';
        EXCEPTION
          WHEN OTHERS THEN
            IF SQLCODE != -955 THEN RAISE;
        END;
        """
    ).strip()


def _oracle_creates() -> list[str]:
    ddls = [
        """CREATE TABLE "dbo"."customers" (
          "customer_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "customer_name" NVARCHAR2(400),
          "email" NVARCHAR2(320),
          "city" NVARCHAR2(200),
          "country_code" NVARCHAR2(16),
          "phone" NVARCHAR2(64),
          "is_active" NUMBER(1),
          "created_at" TIMESTAMP(3)
        )""",
        """CREATE TABLE "dbo"."orders" (
          "order_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "customer_id" NUMBER(10),
          "status" NVARCHAR2(64),
          "amount" NUMBER(18,4),
          "created_at" TIMESTAMP(3),
          "discount" NUMBER(18,4),
          "currency" NVARCHAR2(8),
          "sales_rep_id" NUMBER(10)
        )""",
        """CREATE TABLE "dbo"."order_lines" (
          "line_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "order_id" NUMBER(10),
          "product_id" NUMBER(10),
          "quantity" NUMBER(18,4),
          "unit_price" NUMBER(18,4),
          "discount" NUMBER(18,4),
          "net_amount" NUMBER(18,4)
        )""",
        """CREATE TABLE "finance"."invoices" (
          "invoice_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "order_id" NUMBER(10),
          "amount" NUMBER(18,4),
          "net_amount" NUMBER(18,4),
          "vat_amount" NUMBER(18,4),
          "vat_rate" NUMBER(9,4),
          "status" NVARCHAR2(64),
          "due_date" TIMESTAMP(3),
          "created_at" TIMESTAMP(3)
        )""",
        """CREATE TABLE "finance"."payments" (
          "payment_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "invoice_id" NUMBER(10),
          "amount" NUMBER(18,4),
          "paid_at" TIMESTAMP(3),
          "payment_status" NVARCHAR2(64),
          "currency" NVARCHAR2(8)
        )""",
        """CREATE TABLE "logistics"."shipments" (
          "shipment_id" NUMBER(10) NOT NULL PRIMARY KEY,
          "order_id" NUMBER(10),
          "tracking_number" NVARCHAR2(128),
          "shipment_status" NVARCHAR2(64),
          "shipped_at" TIMESTAMP(3),
          "warehouse_id" NUMBER(10)
        )""",
    ]
    return [_ora_wrap_create(d) for d in ddls]


def _oracle_data() -> list[str]:
    return [
        'DELETE FROM "finance"."payments";',
        'DELETE FROM "logistics"."shipments";',
        'DELETE FROM "finance"."invoices";',
        'DELETE FROM "dbo"."order_lines";',
        'DELETE FROM "dbo"."orders";',
        'DELETE FROM "dbo"."customers";',
        textwrap.dedent(
            """
            INSERT INTO "dbo"."customers" ("customer_id","customer_name","email","city","country_code","phone","is_active","created_at")
            VALUES (1, 'Acme Wholesale', 'orders@acme.example', 'Tel Aviv', 'IL', '+972-3-0000000', 1, CURRENT_TIMESTAMP)
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO "dbo"."orders" ("order_id","customer_id","status","amount","created_at","discount","currency","sales_rep_id")
            VALUES (1, 1, 'open', 250.0000, CURRENT_TIMESTAMP, 0.0000, 'ILS', 10)
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO "dbo"."order_lines" ("line_id","order_id","product_id","quantity","unit_price","discount","net_amount")
            VALUES (1, 1, 1001, 2.0000, 125.0000, 0.0000, 250.0000)
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO "finance"."invoices" ("invoice_id","order_id","amount","net_amount","vat_amount","vat_rate","status","due_date","created_at")
            VALUES (1, 1, 250.0000, 210.0000, 40.0000, 0.17, 'open', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO "finance"."payments" ("payment_id","invoice_id","amount","paid_at","payment_status","currency")
            VALUES (1, 1, 210.0000, CURRENT_TIMESTAMP, 'posted', 'ILS')
            """
        ).strip(),
        textwrap.dedent(
            """
            INSERT INTO "logistics"."shipments" ("shipment_id","order_id","tracking_number","shipment_status","shipped_at","warehouse_id")
            VALUES (1, 1, 'TRK-001', 'in_transit', CURRENT_TIMESTAMP, 7)
            """
        ).strip(),
    ]


def _db2_ddls() -> list[str]:
    return [
        """CREATE TABLE "dbo"."customers" (
          "customer_id" INT NOT NULL PRIMARY KEY,
          "customer_name" VARCHAR(400),
          "email" VARCHAR(320),
          "city" VARCHAR(200),
          "country_code" VARCHAR(16),
          "phone" VARCHAR(64),
          "is_active" SMALLINT,
          "created_at" TIMESTAMP(3)
        )""",
        """CREATE TABLE "dbo"."orders" (
          "order_id" INT NOT NULL PRIMARY KEY,
          "customer_id" INT,
          "status" VARCHAR(64),
          "amount" DECIMAL(18,4),
          "created_at" TIMESTAMP(3),
          "discount" DECIMAL(18,4),
          "currency" VARCHAR(8),
          "sales_rep_id" INT
        )""",
        """CREATE TABLE "dbo"."order_lines" (
          "line_id" INT NOT NULL PRIMARY KEY,
          "order_id" INT,
          "product_id" INT,
          "quantity" DECIMAL(18,4),
          "unit_price" DECIMAL(18,4),
          "discount" DECIMAL(18,4),
          "net_amount" DECIMAL(18,4)
        )""",
        """CREATE TABLE "finance"."invoices" (
          "invoice_id" INT NOT NULL PRIMARY KEY,
          "order_id" INT,
          "amount" DECIMAL(18,4),
          "net_amount" DECIMAL(18,4),
          "vat_amount" DECIMAL(18,4),
          "vat_rate" DECIMAL(9,4),
          "status" VARCHAR(64),
          "due_date" TIMESTAMP(3),
          "created_at" TIMESTAMP(3)
        )""",
        """CREATE TABLE "finance"."payments" (
          "payment_id" INT NOT NULL PRIMARY KEY,
          "invoice_id" INT,
          "amount" DECIMAL(18,4),
          "paid_at" TIMESTAMP(3),
          "payment_status" VARCHAR(64),
          "currency" VARCHAR(8)
        )""",
        """CREATE TABLE "logistics"."shipments" (
          "shipment_id" INT NOT NULL PRIMARY KEY,
          "order_id" INT,
          "tracking_number" VARCHAR(128),
          "shipment_status" VARCHAR(64),
          "shipped_at" TIMESTAMP(3),
          "warehouse_id" INT
        )""",
    ]


def _db2_create_schemas() -> list[str]:
    out: list[str] = []
    for sch in ("FINANCE", "LOGISTICS"):
        out.append(
            textwrap.dedent(
                f"""
                BEGIN
                  DECLARE v_cnt INTEGER DEFAULT 0;
                  SELECT COUNT(*) INTO v_cnt FROM SYSCAT.SCHEMATA
                    WHERE UPPER(SCHEMANAME) = '{sch}' WITH UR;
                  IF v_cnt = 0 THEN
                    EXECUTE IMMEDIATE 'CREATE SCHEMA {sch.lower()}';
                  END IF;
                END
                """
            ).strip()
        )
    return out


def _db2_idempotent_creates() -> list[str]:
    pairs = [
        ("DBO", "CUSTOMERS"),
        ("DBO", "ORDERS"),
        ("DBO", "ORDER_LINES"),
        ("FINANCE", "INVOICES"),
        ("FINANCE", "PAYMENTS"),
        ("LOGISTICS", "SHIPMENTS"),
    ]
    out: list[str] = []
    for (sch, tbl), ddl in zip(pairs, _db2_ddls(), strict=True):
        body = ddl.replace("'", "''")
        out.append(
            textwrap.dedent(
                f"""
                BEGIN
                  DECLARE v_cnt INTEGER DEFAULT 0;
                  SELECT COUNT(*) INTO v_cnt FROM SYSCAT.TABLES
                    WHERE UPPER(TABSCHEMA) = '{sch}' AND UPPER(TABNAME) = '{tbl}' WITH UR;
                  IF v_cnt = 0 THEN
                    EXECUTE IMMEDIATE '{body}';
                  END IF;
                END
                """
            ).strip()
        )
    return out


def _db2_data() -> list[str]:
    return [
        'DELETE FROM "finance"."payments";',
        'DELETE FROM "logistics"."shipments";',
        'DELETE FROM "finance"."invoices";',
        'DELETE FROM "dbo"."order_lines";',
        'DELETE FROM "dbo"."orders";',
        'DELETE FROM "dbo"."customers";',
        """INSERT INTO "dbo"."customers" ("customer_id","customer_name","email","city","country_code","phone","is_active","created_at")
           VALUES (1, 'Acme Wholesale', 'orders@acme.example', 'Tel Aviv', 'IL', '+972-3-0000000', 1, CURRENT_TIMESTAMP)""",
        """INSERT INTO "dbo"."orders" ("order_id","customer_id","status","amount","created_at","discount","currency","sales_rep_id")
           VALUES (1, 1, 'open', 250.0000, CURRENT_TIMESTAMP, 0.0000, 'ILS', 10)""",
        """INSERT INTO "dbo"."order_lines" ("line_id","order_id","product_id","quantity","unit_price","discount","net_amount")
           VALUES (1, 1, 1001, 2.0000, 125.0000, 0.0000, 250.0000)""",
        """INSERT INTO "finance"."invoices" ("invoice_id","order_id","amount","net_amount","vat_amount","vat_rate","status","due_date","created_at")
           VALUES (1, 1, 250.0000, 210.0000, 40.0000, 0.17, 'open', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        """INSERT INTO "finance"."payments" ("payment_id","invoice_id","amount","paid_at","payment_status","currency")
           VALUES (1, 1, 210.0000, CURRENT_TIMESTAMP, 'posted', 'ILS')""",
        """INSERT INTO "logistics"."shipments" ("shipment_id","order_id","tracking_number","shipment_status","shipped_at","warehouse_id")
           VALUES (1, 1, 'TRK-001', 'in_transit', CURRENT_TIMESTAMP, 7)""",
    ]


def deployment_statement_groups(dialect: str) -> tuple[str, list[list[str]]]:
    """
    Ordered statement groups. T-SQL uses one transaction for DDL+DML groups combined in deploy.

    Oracle: first group is DDL (implicit commits); second is DML in one transaction.
    """
    d = dialect.lower().strip()
    if d in ("tsql", "sqlserver"):
        return ("tsql", [_tsql_schemas_and_tables(), _tsql_data_reset(), _tsql_inserts()])
    if d == "oracle":
        return ("oracle", [_oracle_creates(), _oracle_data()])
    if d == "db2":
        return ("db2", [_db2_create_schemas() + _db2_idempotent_creates(), _db2_data()])
    raise ValueError(f"unsupported dialect for Kfar SQL generation: {dialect}")
