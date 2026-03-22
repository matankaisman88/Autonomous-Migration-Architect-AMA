-- GL tie-out: invoices and payments for sales.orders
SELECT
  p.id AS payment_id,
  i.id AS invoice_id,
  o.order_id
FROM finance.payments p
JOIN finance.invoices i ON i.id = p.invoice_id
JOIN sales.orders o ON o.order_id = i.order_id;
