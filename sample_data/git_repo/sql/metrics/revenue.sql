-- Revenue metrics referencing legacy sales.orders
SELECT
  DATE_TRUNC('month', created_at) AS m,
  SUM(amount) AS revenue
FROM sales.orders
WHERE status NOT IN ('cancelled')
GROUP BY 1;

SELECT customer_id, amount
FROM sales.orders
WHERE order_id = :oid;
