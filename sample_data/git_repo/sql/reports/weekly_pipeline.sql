-- Executive pipeline: orders joined to CRM and marketing touchpoints
SELECT
  o.order_id,
  c.name AS customer_name,
  l.stage AS lead_stage
FROM sales.orders o
JOIN sales.customers c ON c.id = o.customer_id
LEFT JOIN crm.leads l ON l.account_id = o.customer_id;
