-- Bridge legacy Hebrew billing to finance.invoices
SELECT h.[חשבונית], h.[סכום], i.invoice_id
FROM legacy_hebrew.חשבוניות h
LEFT JOIN finance.invoices i ON h.[חשבונית] = i.invoice_id;
