/*
  Legacy Hebrew Bridge for Kfar Supply (Live Connection + Self-Healing demo)

  Creates schema legacy_hebrew with Hebrew-named VIEWS (column aliases from the
  glossary) over the English source tables, plus SYNONYMs for short Hebrew
  object names. After this script, T-SQL that references Hebrew identifiers
  compiles and runs; SQL Server records it in Query Store / plan-cache DMVs
  for Live extraction.

  Target DB: kfar_supply
  Requires: finance.*, dbo.*, logistics.* tables from setup_dev_mssql.py
*/
USE kfar_supply;
GO

IF SCHEMA_ID(N'legacy_hebrew') IS NULL
    EXEC(N'CREATE SCHEMA [legacy_hebrew]');
GO

/* -------- Hebrew views (column-level aliases) -------- */

CREATE OR ALTER VIEW legacy_hebrew.[חשבוניות]
AS
SELECT
    invoice_id   AS [חשבונית],
    order_id     AS [הזמנה],
    amount       AS [סכום],
    net_amount   AS [סכום_נטו],
    vat_amount   AS [סכום_מעמ],
    vat_rate     AS [מעמ],
    status       AS [סטטוס],
    due_date     AS [תאריך_פירעון],
    created_at   AS [תאריך_יצירה]
FROM finance.invoices;
GO

CREATE OR ALTER VIEW legacy_hebrew.[לקוחות]
AS
SELECT
    customer_id   AS [לקוח],
    customer_name AS [שם_לקוח],
    email         AS [אימייל],
    city          AS [עיר],
    country_code  AS [קוד_מדינה],
    phone         AS [טלפון],
    is_active     AS [פעיל],
    created_at    AS [תאריך_יצירה]
FROM dbo.customers;
GO

CREATE OR ALTER VIEW legacy_hebrew.[הזמנות]
AS
SELECT
    order_id     AS [הזמנה],
    customer_id  AS [לקוח],
    status       AS [סטטוס],
    amount       AS [סכום],
    created_at   AS [תאריך_יצירה],
    discount     AS [הנחה],
    currency     AS [מטבע],
    sales_rep_id AS [סוכן]
FROM dbo.orders;
GO

CREATE OR ALTER VIEW legacy_hebrew.[שורות_הזמנה]
AS
SELECT
    line_id     AS [מזהה_שורה],
    order_id    AS [הזמנה],
    product_id  AS [מזהה_מוצר],
    quantity    AS [כמות],
    unit_price  AS [מחיר_יחידה],
    discount    AS [הנחה],
    net_amount  AS [סכום_נטו]
FROM dbo.order_lines;
GO

CREATE OR ALTER VIEW legacy_hebrew.[תשלומים]
AS
SELECT
    payment_id     AS [תשלום],
    invoice_id     AS [חשבונית],
    amount         AS [סכום],
    paid_at        AS [תאריך_תשלום],
    payment_status AS [סטטוס_תשלום],
    currency       AS [מטבע]
FROM finance.payments;
GO

CREATE OR ALTER VIEW legacy_hebrew.[משלוחים]
AS
SELECT
    shipment_id     AS [מזהה_משלוח],
    order_id        AS [הזמנה],
    tracking_number AS [מספר_מעקב],
    shipment_status AS [סטטוס_משלוח],
    shipped_at      AS [תאריך_משלוח],
    warehouse_id    AS [מחסן]
FROM logistics.shipments;
GO

/* -------- Synonyms (short Hebrew names → English base tables) -------- */

IF OBJECT_ID(N'legacy_hebrew.חשבוניות_syn', N'SN') IS NOT NULL
    DROP SYNONYM legacy_hebrew.[חשבוניות_syn];
CREATE SYNONYM legacy_hebrew.[חשבוניות_syn] FOR finance.invoices;
GO

IF OBJECT_ID(N'legacy_hebrew.לקוחות_syn', N'SN') IS NOT NULL
    DROP SYNONYM legacy_hebrew.[לקוחות_syn];
CREATE SYNONYM legacy_hebrew.[לקוחות_syn] FOR dbo.customers;
GO

IF OBJECT_ID(N'legacy_hebrew.הזמנות_syn', N'SN') IS NOT NULL
    DROP SYNONYM legacy_hebrew.[הזמנות_syn];
CREATE SYNONYM legacy_hebrew.[הזמנות_syn] FOR dbo.orders;
GO

IF OBJECT_ID(N'legacy_hebrew.תשלומים_syn', N'SN') IS NOT NULL
    DROP SYNONYM legacy_hebrew.[תשלומים_syn];
CREATE SYNONYM legacy_hebrew.[תשלומים_syn] FOR finance.payments;
GO

IF OBJECT_ID(N'legacy_hebrew.משלוחים_syn', N'SN') IS NOT NULL
    DROP SYNONYM legacy_hebrew.[משלוחים_syn];
CREATE SYNONYM legacy_hebrew.[משלוחים_syn] FOR logistics.shipments;
GO

/* -------- Smoke query (bridge join to modern finance) -------- */
SELECT h.[חשבונית], h.[סכום], i.invoice_id
FROM legacy_hebrew.[חשבוניות] h
LEFT JOIN finance.invoices i ON h.[חשבונית] = i.invoice_id;
GO
