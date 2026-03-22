-- Bridge query during dual-write: legacy Hebrew store + modern finance
SELECT h.id, h.DATA
FROM legacy_hebrew.חשבוניות h
INNER JOIN finance.invoices i ON i.order_id = h.id
WHERE i.status = 'posted';
