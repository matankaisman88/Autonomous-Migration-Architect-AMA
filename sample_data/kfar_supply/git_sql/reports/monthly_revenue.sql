-- Kfar Supply: revenue roll-up (orders + invoices)
SELECT o.order_id, o.amount AS order_amt, i.net_amount
FROM dbo.orders o
JOIN finance.invoices i ON o.order_id = i.order_id
WHERE o.status <> N'cancelled';
