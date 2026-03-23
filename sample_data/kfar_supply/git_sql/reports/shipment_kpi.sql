-- Shipment KPIs vs orders
SELECT s.shipment_id, s.tracking_number, o.order_id
FROM logistics.shipments s
INNER JOIN dbo.orders o ON s.order_id = o.order_id;
