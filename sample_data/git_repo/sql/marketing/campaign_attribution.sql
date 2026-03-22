-- Attribution: promotional touch to order lines
SELECT
  ol.order_id,
  pr.name AS promotion_name
FROM sales.order_lines ol
JOIN marketing.promotions pr ON pr.id = ol.promo_id
JOIN sales.orders o ON o.order_id = ol.order_id;
