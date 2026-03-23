-- Customer activity
SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS cnt
FROM dbo.customers c
LEFT JOIN dbo.orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.customer_name;
