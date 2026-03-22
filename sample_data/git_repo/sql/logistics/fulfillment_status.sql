-- Operational: shipment state vs order state
SELECT
  s.tracking_no,
  o.order_id,
  o.status AS order_status
FROM logistics.shipments s
JOIN sales.orders o ON o.order_id = s.order_id;
