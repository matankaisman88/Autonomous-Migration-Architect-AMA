/*
  Kfar Supply — 100 read-only test queries for Real Extraction / Query Store.

  Usage (SSMS):
    1. Connect to your Kfar database (e.g. kfar_supply).
    2. Paste this entire script (or open this file).
    3. Execute (F5). Queries 1–70 use dbo only; 71–100 need finance + logistics schemas.

  After running, re-run Live → Real Extraction with today's log end date.
*/

USE kfar_supply;
GO

-- dbo.customers (1–15)
SELECT TOP 100 customer_id, customer_name, email FROM dbo.customers;
GO
SELECT customer_id, city, country_code FROM dbo.customers WHERE is_active = 1;
GO
SELECT customer_name, email, phone FROM dbo.customers WHERE city = N'Tel Aviv';
GO
SELECT country_code, COUNT(*) AS customer_count FROM dbo.customers GROUP BY country_code;
GO
SELECT TOP 50 customer_id, created_at FROM dbo.customers ORDER BY created_at DESC;
GO
SELECT customer_id, customer_name FROM dbo.customers WHERE email LIKE N'%@%';
GO
SELECT DISTINCT country_code FROM dbo.customers ORDER BY country_code;
GO
SELECT customer_id, customer_name FROM dbo.customers WHERE is_active = 0;
GO
SELECT TOP 20 customer_name, city FROM dbo.customers WHERE country_code = N'IL';
GO
SELECT AVG(CAST(is_active AS float)) AS active_rate FROM dbo.customers;
GO
SELECT customer_id FROM dbo.customers WHERE phone IS NOT NULL;
GO
SELECT city, COUNT(*) AS cnt FROM dbo.customers GROUP BY city HAVING COUNT(*) > 1;
GO
SELECT TOP 100 * FROM dbo.customers WHERE customer_id BETWEEN 1 AND 500;
GO
SELECT customer_name, email FROM dbo.customers WHERE created_at >= DATEADD(day, -30, GETDATE());
GO
SELECT customer_id, customer_name FROM dbo.customers WHERE customer_name LIKE N'A%';
GO

-- dbo.orders (16–35)
SELECT TOP 100 order_id, customer_id, status, amount FROM dbo.orders;
GO
SELECT order_id, amount, currency FROM dbo.orders WHERE status = N'Pending';
GO
SELECT customer_id, SUM(amount) AS total_spent FROM dbo.orders GROUP BY customer_id;
GO
SELECT status, COUNT(*) AS order_count FROM dbo.orders GROUP BY status;
GO
SELECT TOP 50 order_id, created_at FROM dbo.orders ORDER BY amount DESC;
GO
SELECT order_id, discount FROM dbo.orders WHERE discount > 0;
GO
SELECT currency, SUM(amount) AS revenue FROM dbo.orders GROUP BY currency;
GO
SELECT order_id, customer_id FROM dbo.orders WHERE sales_rep_id IS NOT NULL;
GO
SELECT TOP 100 * FROM dbo.orders WHERE created_at >= DATEADD(month, -1, GETDATE());
GO
SELECT customer_id, COUNT(*) AS order_count FROM dbo.orders GROUP BY customer_id HAVING COUNT(*) > 5;
GO
SELECT order_id, amount FROM dbo.orders WHERE amount > 1000;
GO
SELECT DISTINCT status FROM dbo.orders;
GO
SELECT sales_rep_id, COUNT(*) AS orders_handled FROM dbo.orders GROUP BY sales_rep_id;
GO
SELECT TOP 20 order_id, status, amount FROM dbo.orders WHERE currency = N'USD';
GO
SELECT order_id FROM dbo.orders WHERE status IN (N'Shipped', N'Completed', N'Pending');
GO
SELECT AVG(amount) AS avg_order_value FROM dbo.orders;
GO
SELECT customer_id, MAX(amount) AS max_order FROM dbo.orders GROUP BY customer_id;
GO
SELECT order_id, amount, discount FROM dbo.orders WHERE discount IS NOT NULL AND discount > 0;
GO
SELECT TOP 100 order_id, customer_id, created_at FROM dbo.orders ORDER BY order_id;
GO
SELECT COUNT(*) AS open_orders FROM dbo.orders WHERE status <> N'Completed';
GO

-- dbo.order_lines (36–50)
SELECT TOP 100 line_id, order_id, product_id, quantity FROM dbo.order_lines;
GO
SELECT order_id, SUM(quantity) AS total_qty FROM dbo.order_lines GROUP BY order_id;
GO
SELECT product_id, SUM(quantity) AS units_sold FROM dbo.order_lines GROUP BY product_id;
GO
SELECT line_id, unit_price, net_amount FROM dbo.order_lines WHERE net_amount > 100;
GO
SELECT order_id, COUNT(*) AS line_count FROM dbo.order_lines GROUP BY order_id;
GO
SELECT TOP 50 * FROM dbo.order_lines ORDER BY net_amount DESC;
GO
SELECT product_id, AVG(unit_price) AS avg_price FROM dbo.order_lines GROUP BY product_id;
GO
SELECT line_id, order_id FROM dbo.order_lines WHERE quantity >= 10;
GO
SELECT order_id, SUM(net_amount) AS order_line_total FROM dbo.order_lines GROUP BY order_id;
GO
SELECT TOP 100 line_id, discount FROM dbo.order_lines WHERE discount > 0;
GO
SELECT product_id, COUNT(DISTINCT order_id) AS order_count FROM dbo.order_lines GROUP BY product_id;
GO
SELECT line_id, quantity, unit_price, (quantity * unit_price) AS gross FROM dbo.order_lines;
GO
SELECT order_id FROM dbo.order_lines GROUP BY order_id HAVING SUM(quantity) > 20;
GO
SELECT TOP 20 product_id, SUM(net_amount) AS revenue FROM dbo.order_lines GROUP BY product_id ORDER BY revenue DESC;
GO
SELECT * FROM dbo.order_lines WHERE order_id = (SELECT TOP 1 order_id FROM dbo.orders ORDER BY order_id DESC);
GO

-- dbo joins (51–70)
SELECT o.order_id, o.amount, c.customer_name FROM dbo.orders o INNER JOIN dbo.customers c ON o.customer_id = c.customer_id;
GO
SELECT c.customer_name, o.order_id, o.status FROM dbo.customers c INNER JOIN dbo.orders o ON c.customer_id = o.customer_id;
GO
SELECT o.order_id, ol.line_id, ol.product_id, ol.quantity FROM dbo.orders o INNER JOIN dbo.order_lines ol ON o.order_id = ol.order_id;
GO
SELECT c.customer_name, o.order_id, ol.net_amount FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id;
GO
SELECT o.order_id, SUM(ol.net_amount) AS line_total FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id;
GO
SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS orders FROM dbo.customers c LEFT JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.customer_id, c.customer_name;
GO
SELECT o.order_id, o.amount, c.city FROM dbo.orders o JOIN dbo.customers c ON o.customer_id = c.customer_id WHERE c.country_code = N'IL';
GO
SELECT c.customer_name, SUM(o.amount) AS lifetime_value FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.customer_name;
GO
SELECT o.order_id, o.status, ol.product_id FROM dbo.orders o LEFT JOIN dbo.order_lines ol ON o.order_id = ol.order_id WHERE o.status = N'Pending';
GO
SELECT TOP 100 c.email, o.order_id, o.created_at FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id ORDER BY o.created_at DESC;
GO
SELECT o.customer_id, AVG(o.amount) AS avg_order, COUNT(ol.line_id) AS line_count FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.customer_id;
GO
SELECT c.customer_id FROM dbo.customers c WHERE NOT EXISTS (SELECT 1 FROM dbo.orders o WHERE o.customer_id = c.customer_id);
GO
SELECT o.order_id FROM dbo.orders o WHERE EXISTS (SELECT 1 FROM dbo.order_lines ol WHERE ol.order_id = o.order_id AND ol.quantity > 5);
GO
SELECT c.customer_name, o.order_id, ol.quantity, ol.unit_price FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id WHERE o.amount > 500;
GO
SELECT o.order_id, c.customer_name, o.amount, o.currency FROM dbo.orders o INNER JOIN dbo.customers c ON o.customer_id = c.customer_id WHERE o.discount IS NOT NULL;
GO
SELECT TOP 50 c.city, SUM(o.amount) AS city_revenue FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.city ORDER BY city_revenue DESC;
GO
SELECT o.order_id, COUNT(ol.line_id) AS lines, SUM(ol.net_amount) AS net FROM dbo.orders o LEFT JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id;
GO
SELECT c.customer_id, c.customer_name, o.order_id, o.status FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id WHERE c.is_active = 1;
GO
SELECT ol.product_id, c.country_code, SUM(ol.quantity) AS qty FROM dbo.order_lines ol JOIN dbo.orders o ON ol.order_id = o.order_id JOIN dbo.customers c ON o.customer_id = c.customer_id GROUP BY ol.product_id, c.country_code;
GO
SELECT o.order_id, o.customer_id, o.amount FROM dbo.orders o WHERE o.customer_id IN (SELECT customer_id FROM dbo.customers WHERE is_active = 1);
GO

-- finance + logistics (71–100) — skip if those schemas are not deployed
SELECT TOP 100 invoice_id, order_id, amount, status FROM finance.invoices;
GO
SELECT order_id, SUM(net_amount) AS invoiced FROM finance.invoices GROUP BY order_id;
GO
SELECT status, COUNT(*) FROM finance.invoices GROUP BY status;
GO
SELECT invoice_id, vat_amount, vat_rate FROM finance.invoices WHERE vat_rate > 0;
GO
SELECT TOP 50 * FROM finance.invoices WHERE due_date < GETDATE() AND status <> N'Paid';
GO
SELECT payment_id, invoice_id, amount, payment_status FROM finance.payments;
GO
SELECT invoice_id, SUM(amount) AS paid FROM finance.payments GROUP BY invoice_id;
GO
SELECT payment_status, COUNT(*) FROM finance.payments GROUP BY payment_status;
GO
SELECT TOP 100 shipment_id, order_id, tracking_number, shipment_status FROM logistics.shipments;
GO
SELECT warehouse_id, COUNT(*) FROM logistics.shipments GROUP BY warehouse_id;
GO
SELECT o.order_id, i.invoice_id, i.amount FROM dbo.orders o INNER JOIN finance.invoices i ON o.order_id = i.order_id;
GO
SELECT o.order_id, o.status, i.net_amount, i.status AS invoice_status FROM dbo.orders o JOIN finance.invoices i ON o.order_id = i.order_id;
GO
SELECT i.invoice_id, p.payment_id, p.amount FROM finance.invoices i LEFT JOIN finance.payments p ON i.invoice_id = p.invoice_id;
GO
SELECT o.order_id, s.tracking_number, s.shipment_status FROM dbo.orders o JOIN logistics.shipments s ON o.order_id = s.order_id;
GO
SELECT c.customer_name, o.order_id, i.invoice_id, i.amount FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN finance.invoices i ON o.order_id = i.order_id;
GO
SELECT o.order_id, ol.line_id, i.invoice_id FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id JOIN finance.invoices i ON o.order_id = i.order_id;
GO
SELECT i.invoice_id, i.amount, p.paid_at FROM finance.invoices i JOIN finance.payments p ON i.invoice_id = p.invoice_id;
GO
SELECT o.order_id, s.shipped_at, s.warehouse_id FROM dbo.orders o LEFT JOIN logistics.shipments s ON o.order_id = s.order_id WHERE o.status = N'Shipped';
GO
SELECT TOP 100 o.order_id, c.customer_name, i.net_amount, p.payment_status FROM dbo.orders o JOIN dbo.customers c ON o.customer_id = c.customer_id JOIN finance.invoices i ON o.order_id = i.order_id LEFT JOIN finance.payments p ON i.invoice_id = p.invoice_id;
GO
SELECT o.order_id, SUM(ol.net_amount) AS lines_total, MAX(i.amount) AS invoice_amount FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id JOIN finance.invoices i ON o.order_id = i.order_id GROUP BY o.order_id;
GO
SELECT i.status, SUM(i.amount) AS total FROM finance.invoices i GROUP BY i.status;
GO
SELECT s.shipment_status, COUNT(DISTINCT s.order_id) FROM logistics.shipments s GROUP BY s.shipment_status;
GO
SELECT c.customer_id, SUM(i.net_amount) AS total_invoiced FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN finance.invoices i ON o.order_id = i.order_id GROUP BY c.customer_id;
GO
SELECT o.order_id FROM dbo.orders o WHERE EXISTS (SELECT 1 FROM finance.invoices i WHERE i.order_id = o.order_id AND i.status = N'Open');
GO
SELECT o.order_id FROM dbo.orders o WHERE EXISTS (SELECT 1 FROM logistics.shipments s WHERE s.order_id = o.order_id);
GO
SELECT i.invoice_id, i.order_id, i.due_date FROM finance.invoices i WHERE i.created_at >= DATEADD(day, -7, GETDATE());
GO
SELECT p.currency, SUM(p.amount) FROM finance.payments p GROUP BY p.currency;
GO
SELECT TOP 20 o.order_id, o.amount, s.tracking_number FROM dbo.orders o JOIN logistics.shipments s ON o.order_id = s.order_id ORDER BY s.shipped_at DESC;
GO
SELECT c.customer_name, o.order_id, ol.product_id, i.invoice_id, s.tracking_number FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id JOIN finance.invoices i ON o.order_id = i.order_id LEFT JOIN logistics.shipments s ON o.order_id = s.order_id;
GO
SELECT o.order_id, o.amount, i.net_amount, p.amount AS paid_amount FROM dbo.orders o JOIN finance.invoices i ON o.order_id = i.order_id LEFT JOIN finance.payments p ON i.invoice_id = p.invoice_id WHERE o.created_at >= DATEADD(month, -3, GETDATE());
GO
