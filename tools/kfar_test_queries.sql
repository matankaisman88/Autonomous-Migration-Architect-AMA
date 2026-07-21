/*
  Kfar Supply — 100 read-only test queries (dbo only) for Real Extraction / Query Store.

  SSMS: open this file, fix USE database name if needed, press F5.
  Each batch includes a unique comment (ama-test-qNNN) so Query Store / plan cache
  keep all 100 as distinct entries (dedupe will not collapse them).

  Expect: 100 x "(N rows affected)" in Messages (N may be 0 — that is OK).
*/

USE kfar_supply;
GO

/* ama-test-q001 */ SELECT TOP 100 customer_id, customer_name, email FROM dbo.customers;
GO
/* ama-test-q002 */ SELECT customer_id, city, country_code FROM dbo.customers WHERE is_active = 1;
GO
/* ama-test-q003 */ SELECT customer_name, email, phone FROM dbo.customers WHERE city = N'Tel Aviv';
GO
/* ama-test-q004 */ SELECT country_code, COUNT(*) AS customer_count FROM dbo.customers GROUP BY country_code;
GO
/* ama-test-q005 */ SELECT TOP 50 customer_id, created_at FROM dbo.customers ORDER BY created_at DESC;
GO
/* ama-test-q006 */ SELECT customer_id, customer_name FROM dbo.customers WHERE email LIKE N'%@%';
GO
/* ama-test-q007 */ SELECT DISTINCT country_code FROM dbo.customers ORDER BY country_code;
GO
/* ama-test-q008 */ SELECT customer_id, customer_name FROM dbo.customers WHERE is_active = 0;
GO
/* ama-test-q009 */ SELECT TOP 20 customer_name, city FROM dbo.customers WHERE country_code = N'IL';
GO
/* ama-test-q010 */ SELECT AVG(CAST(is_active AS float)) AS active_rate FROM dbo.customers;
GO
/* ama-test-q011 */ SELECT customer_id FROM dbo.customers WHERE phone IS NOT NULL;
GO
/* ama-test-q012 */ SELECT city, COUNT(*) AS cnt FROM dbo.customers GROUP BY city HAVING COUNT(*) > 1;
GO
/* ama-test-q013 */ SELECT TOP 100 * FROM dbo.customers WHERE customer_id BETWEEN 1 AND 500;
GO
/* ama-test-q014 */ SELECT customer_name, email FROM dbo.customers WHERE created_at >= DATEADD(day, -30, GETDATE());
GO
/* ama-test-q015 */ SELECT customer_id, customer_name FROM dbo.customers WHERE customer_name LIKE N'A%';
GO
/* ama-test-q016 */ SELECT TOP 100 order_id, customer_id, status, amount FROM dbo.orders;
GO
/* ama-test-q017 */ SELECT order_id, amount, currency FROM dbo.orders WHERE status = N'Pending';
GO
/* ama-test-q018 */ SELECT customer_id, SUM(TRY_CAST(amount AS DECIMAL(18,4))) AS total_spent FROM dbo.orders GROUP BY customer_id;
GO
/* ama-test-q019 */ SELECT status, COUNT(*) AS order_count FROM dbo.orders GROUP BY status;
GO
/* ama-test-q020 */ SELECT TOP 50 order_id, created_at FROM dbo.orders ORDER BY TRY_CAST(amount AS DECIMAL(18,4)) DESC;
GO
/* ama-test-q021 */ SELECT order_id, discount FROM dbo.orders WHERE TRY_CAST(discount AS DECIMAL(18,4)) > 0;
GO
/* ama-test-q022 */ SELECT currency, SUM(TRY_CAST(amount AS DECIMAL(18,4))) AS revenue FROM dbo.orders GROUP BY currency;
GO
/* ama-test-q023 */ SELECT order_id, customer_id FROM dbo.orders WHERE sales_rep_id IS NOT NULL;
GO
/* ama-test-q024 */ SELECT TOP 100 * FROM dbo.orders WHERE created_at >= DATEADD(month, -1, GETDATE());
GO
/* ama-test-q025 */ SELECT customer_id, COUNT(*) AS order_count FROM dbo.orders GROUP BY customer_id HAVING COUNT(*) > 5;
GO
/* ama-test-q026 */ SELECT order_id, amount FROM dbo.orders WHERE TRY_CAST(amount AS DECIMAL(18,4)) > 1000;
GO
/* ama-test-q027 */ SELECT DISTINCT status FROM dbo.orders;
GO
/* ama-test-q028 */ SELECT sales_rep_id, COUNT(*) AS orders_handled FROM dbo.orders GROUP BY sales_rep_id;
GO
/* ama-test-q029 */ SELECT TOP 20 order_id, status, amount FROM dbo.orders WHERE currency = N'USD';
GO
/* ama-test-q030 */ SELECT order_id FROM dbo.orders WHERE status IN (N'Shipped', N'Completed', N'Pending');
GO
/* ama-test-q031 */ SELECT AVG(TRY_CAST(amount AS DECIMAL(18,4))) AS avg_order_value FROM dbo.orders;
GO
/* ama-test-q032 */ SELECT customer_id, MAX(TRY_CAST(amount AS DECIMAL(18,4))) AS max_order FROM dbo.orders GROUP BY customer_id;
GO
/* ama-test-q033 */ SELECT order_id, amount, discount FROM dbo.orders WHERE discount IS NOT NULL AND TRY_CAST(discount AS DECIMAL(18,4)) > 0;
GO
/* ama-test-q034 */ SELECT TOP 100 order_id, customer_id, created_at FROM dbo.orders ORDER BY order_id;
GO
/* ama-test-q035 */ SELECT COUNT(*) AS open_orders FROM dbo.orders WHERE status <> N'Completed';
GO
/* ama-test-q036 */ SELECT TOP 100 line_id, order_id, product_id, quantity FROM dbo.order_lines;
GO
/* ama-test-q037 */ SELECT order_id, SUM(TRY_CAST(quantity AS DECIMAL(18,4))) AS total_qty FROM dbo.order_lines GROUP BY order_id;
GO
/* ama-test-q038 */ SELECT product_id, SUM(TRY_CAST(quantity AS DECIMAL(18,4))) AS units_sold FROM dbo.order_lines GROUP BY product_id;
GO
/* ama-test-q039 */ SELECT line_id, unit_price, net_amount FROM dbo.order_lines WHERE TRY_CAST(net_amount AS DECIMAL(18,4)) > 100;
GO
/* ama-test-q040 */ SELECT order_id, COUNT(*) AS line_count FROM dbo.order_lines GROUP BY order_id;
GO
/* ama-test-q041 */ SELECT TOP 50 * FROM dbo.order_lines ORDER BY TRY_CAST(net_amount AS DECIMAL(18,4)) DESC;
GO
/* ama-test-q042 */ SELECT product_id, AVG(TRY_CAST(unit_price AS DECIMAL(18,4))) AS avg_price FROM dbo.order_lines GROUP BY product_id;
GO
/* ama-test-q043 */ SELECT line_id, order_id FROM dbo.order_lines WHERE TRY_CAST(quantity AS DECIMAL(18,4)) >= 10;
GO
/* ama-test-q044 */ SELECT order_id, SUM(TRY_CAST(net_amount AS DECIMAL(18,4))) AS order_line_total FROM dbo.order_lines GROUP BY order_id;
GO
/* ama-test-q045 */ SELECT TOP 100 line_id, discount FROM dbo.order_lines WHERE TRY_CAST(discount AS DECIMAL(18,4)) > 0;
GO
/* ama-test-q046 */ SELECT product_id, COUNT(DISTINCT order_id) AS order_count FROM dbo.order_lines GROUP BY product_id;
GO
/* ama-test-q047 */ SELECT line_id, quantity, unit_price, (TRY_CAST(quantity AS DECIMAL(18,4)) * TRY_CAST(unit_price AS DECIMAL(18,4))) AS gross FROM dbo.order_lines;
GO
/* ama-test-q048 */ SELECT order_id FROM dbo.order_lines GROUP BY order_id HAVING SUM(TRY_CAST(quantity AS DECIMAL(18,4))) > 20;
GO
/* ama-test-q049 */ SELECT TOP 20 product_id, SUM(TRY_CAST(net_amount AS DECIMAL(18,4))) AS revenue FROM dbo.order_lines GROUP BY product_id ORDER BY revenue DESC;
GO
/* ama-test-q050 */ SELECT * FROM dbo.order_lines WHERE order_id = (SELECT TOP 1 order_id FROM dbo.orders ORDER BY order_id DESC);
GO
/* ama-test-q051 */ SELECT o.order_id, o.amount, c.customer_name FROM dbo.orders o INNER JOIN dbo.customers c ON o.customer_id = c.customer_id;
GO
/* ama-test-q052 */ SELECT c.customer_name, o.order_id, o.status FROM dbo.customers c INNER JOIN dbo.orders o ON c.customer_id = o.customer_id;
GO
/* ama-test-q053 */ SELECT o.order_id, ol.line_id, ol.product_id, ol.quantity FROM dbo.orders o INNER JOIN dbo.order_lines ol ON o.order_id = ol.order_id;
GO
/* ama-test-q054 */ SELECT c.customer_name, o.order_id, ol.net_amount FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id;
GO
/* ama-test-q055 */ SELECT o.order_id, SUM(TRY_CAST(ol.net_amount AS DECIMAL(18,4))) AS line_total FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id;
GO
/* ama-test-q056 */ SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS orders FROM dbo.customers c LEFT JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.customer_id, c.customer_name;
GO
/* ama-test-q057 */ SELECT o.order_id, o.amount, c.city FROM dbo.orders o JOIN dbo.customers c ON o.customer_id = c.customer_id WHERE c.country_code = N'IL';
GO
/* ama-test-q058 */ SELECT c.customer_name, SUM(TRY_CAST(o.amount AS DECIMAL(18,4))) AS lifetime_value FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.customer_name;
GO
/* ama-test-q059 */ SELECT o.order_id, o.status, ol.product_id FROM dbo.orders o LEFT JOIN dbo.order_lines ol ON o.order_id = ol.order_id WHERE o.status = N'Pending';
GO
/* ama-test-q060 */ SELECT TOP 100 c.email, o.order_id, o.created_at FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id ORDER BY o.created_at DESC;
GO
/* ama-test-q061 */ SELECT o.customer_id, AVG(TRY_CAST(o.amount AS DECIMAL(18,4))) AS avg_order, COUNT(ol.line_id) AS line_count FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.customer_id;
GO
/* ama-test-q062 */ SELECT c.customer_id FROM dbo.customers c WHERE NOT EXISTS (SELECT 1 FROM dbo.orders o WHERE o.customer_id = c.customer_id);
GO
/* ama-test-q063 */ SELECT o.order_id FROM dbo.orders o WHERE EXISTS (SELECT 1 FROM dbo.order_lines ol WHERE ol.order_id = o.order_id AND TRY_CAST(ol.quantity AS DECIMAL(18,4)) > 5);
GO
/* ama-test-q064 */ SELECT c.customer_name, o.order_id, ol.quantity, ol.unit_price FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id WHERE TRY_CAST(o.amount AS DECIMAL(18,4)) > 500;
GO
/* ama-test-q065 */ SELECT o.order_id, c.customer_name, o.amount, o.currency FROM dbo.orders o INNER JOIN dbo.customers c ON o.customer_id = c.customer_id WHERE o.discount IS NOT NULL;
GO
/* ama-test-q066 */ SELECT TOP 50 c.city, SUM(TRY_CAST(o.amount AS DECIMAL(18,4))) AS city_revenue FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.city ORDER BY city_revenue DESC;
GO
/* ama-test-q067 */ SELECT o.order_id, COUNT(ol.line_id) AS lines, SUM(TRY_CAST(ol.net_amount AS DECIMAL(18,4))) AS net FROM dbo.orders o LEFT JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id;
GO
/* ama-test-q068 */ SELECT c.customer_id, c.customer_name, o.order_id, o.status FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id WHERE c.is_active = 1;
GO
/* ama-test-q069 */ SELECT ol.product_id, c.country_code, SUM(TRY_CAST(ol.quantity AS DECIMAL(18,4))) AS qty FROM dbo.order_lines ol JOIN dbo.orders o ON ol.order_id = o.order_id JOIN dbo.customers c ON o.customer_id = c.customer_id GROUP BY ol.product_id, c.country_code;
GO
/* ama-test-q070 */ SELECT o.order_id, o.customer_id, o.amount FROM dbo.orders o WHERE o.customer_id IN (SELECT customer_id FROM dbo.customers WHERE is_active = 1);
GO
/* ama-test-q071 */ SELECT TOP 10 c.customer_name, o.order_id, o.status, o.amount FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id ORDER BY o.order_id;
GO
/* ama-test-q072 */ SELECT TOP 10 o.order_id, ol.line_id, ol.net_amount FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id ORDER BY ol.line_id;
GO
/* ama-test-q073 */ SELECT c.customer_id, MIN(o.created_at) AS first_order_at FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id GROUP BY c.customer_id;
GO
/* ama-test-q074 */ SELECT o.status, AVG(TRY_CAST(o.amount AS DECIMAL(18,4))) AS avg_amount FROM dbo.orders o GROUP BY o.status;
GO
/* ama-test-q075 */ SELECT ol.product_id, MAX(TRY_CAST(ol.unit_price AS DECIMAL(18,4))) AS max_price FROM dbo.order_lines ol GROUP BY ol.product_id;
GO
/* ama-test-q076 */ SELECT TOP 5 customer_id, customer_name FROM dbo.customers ORDER BY customer_id;
GO
/* ama-test-q077 */ SELECT TOP 5 order_id, status FROM dbo.orders ORDER BY order_id DESC;
GO
/* ama-test-q078 */ SELECT TOP 5 line_id, order_id FROM dbo.order_lines ORDER BY line_id;
GO
/* ama-test-q079 */ SELECT COUNT(*) AS customer_total FROM dbo.customers;
GO
/* ama-test-q080 */ SELECT COUNT(*) AS order_total FROM dbo.orders;
GO
/* ama-test-q081 */ SELECT COUNT(*) AS order_line_total FROM dbo.order_lines;
GO
/* ama-test-q082 */ SELECT TOP 1 customer_name FROM dbo.customers ORDER BY NEWID();
GO
/* ama-test-q083 */ SELECT TOP 1 order_id, amount FROM dbo.orders ORDER BY NEWID();
GO
/* ama-test-q084 */ SELECT c.customer_name, COUNT(DISTINCT ol.product_id) AS products_bought FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY c.customer_name;
GO
/* ama-test-q085 */ SELECT o.order_id, o.status, COUNT(ol.line_id) AS line_items FROM dbo.orders o LEFT JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id, o.status;
GO
/* ama-test-q086 */ SELECT customer_id, email FROM dbo.customers WHERE email IS NOT NULL AND email <> N'';
GO
/* ama-test-q087 */ SELECT order_id, currency FROM dbo.orders WHERE currency IS NOT NULL GROUP BY order_id, currency;
GO
/* ama-test-q088 */ SELECT DISTINCT product_id FROM dbo.order_lines ORDER BY product_id;
GO
/* ama-test-q089 */ SELECT o.order_id FROM dbo.orders o WHERE o.customer_id = (SELECT TOP 1 customer_id FROM dbo.customers ORDER BY customer_id);
GO
/* ama-test-q090 */ SELECT c.customer_name, o.order_id FROM dbo.customers c INNER JOIN dbo.orders o ON c.customer_id = o.customer_id WHERE o.status = N'Completed';
GO
/* ama-test-q091 */ SELECT o.order_id, SUM(TRY_CAST(ol.quantity AS DECIMAL(18,4))) AS units FROM dbo.orders o JOIN dbo.order_lines ol ON o.order_id = ol.order_id GROUP BY o.order_id;
GO
/* ama-test-q092 */ SELECT c.city, COUNT(DISTINCT c.customer_id) AS customers FROM dbo.customers c GROUP BY c.city;
GO
/* ama-test-q093 */ SELECT o.sales_rep_id, SUM(TRY_CAST(o.amount AS DECIMAL(18,4))) AS rep_total FROM dbo.orders o WHERE o.sales_rep_id IS NOT NULL GROUP BY o.sales_rep_id;
GO
/* ama-test-q094 */ SELECT ol.order_id, AVG(TRY_CAST(ol.net_amount AS DECIMAL(18,4))) AS avg_line_net FROM dbo.order_lines ol GROUP BY ol.order_id;
GO
/* ama-test-q095 */ SELECT TOP 25 c.customer_name, o.order_id, ol.product_id FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id;
GO
/* ama-test-q096 */ SELECT o.order_id, c.customer_name, o.created_at FROM dbo.orders o JOIN dbo.customers c ON o.customer_id = c.customer_id WHERE o.created_at IS NOT NULL;
GO
/* ama-test-q097 */ SELECT product_id, COUNT(*) AS line_rows FROM dbo.order_lines GROUP BY product_id HAVING COUNT(*) >= 1;
GO
/* ama-test-q098 */ SELECT c.customer_id, o.order_id, o.amount, ol.line_id FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id JOIN dbo.order_lines ol ON o.order_id = ol.order_id WHERE c.is_active = 1;
GO
/* ama-test-q099 */ SELECT TOP 100 o.order_id, o.customer_id, o.status, o.amount, o.currency FROM dbo.orders o WHERE o.order_id IS NOT NULL;
GO
/* ama-test-q100 */ SELECT 100 AS ama_test_query_count, DB_NAME() AS database_name, SCHEMA_NAME() AS default_schema;
GO
