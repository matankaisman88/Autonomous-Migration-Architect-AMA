-- Fact table grain: order line revenue attributed to finance close
SELECT
  o.order_id,
  o.customer_id,
  o.amount AS order_amount,
  i.total AS invoice_total
FROM sales.orders o
LEFT JOIN finance.invoices i ON i.order_id = o.order_id
WHERE o.status NOT IN ('cancelled', 'void');
