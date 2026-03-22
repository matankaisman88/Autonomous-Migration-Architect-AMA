-- clean
SELECT order_id FROM sales.orders;
SELECT customer_id, amount FROM sales.orders WHERE status = 'x';
