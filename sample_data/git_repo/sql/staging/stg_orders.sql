-- Staging layer: land raw extracts before typing (mirrors legacy sales.orders)
SELECT
  order_id,
  customer_id,
  status,
  amount,
  created_at
FROM sales.orders
WHERE created_at >= CURRENT_DATE - INTERVAL '90 days';
