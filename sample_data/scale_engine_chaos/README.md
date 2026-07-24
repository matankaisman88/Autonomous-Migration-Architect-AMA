# Scale Engine Chaos Dataset

Deterministic synthetic dataset for stress-testing Scale Engine scoring, anomalies, and queueing.

| Table | Expected Queue | Anomaly Flags | Confidence | Criticality | Notes |
|-------|----------------|---------------|------------|-------------|-------|
| finance.core_ledger | RED | none | ~90 | 100 | Criticality override |
| logistics.delivery_status | GREEN | INFO(null-rate skipped) | ~100 | ~0 | High confidence, low impact |
| finance.payment_staging | RED | none | ~85 | >=80 | Criticality wins over confidence band |
| finance.invoice_attachments | RED | BLOCK(unsupported_blob_type) | ~60 | ~10 | VARBINARY block |
| legacy.document_archive | RED | BLOCK(unsupported_blob_type) | ~60 | ~10 | NTEXT block |
| sales.orders | RED | BLOCK(cluster_type_inconsistency) | ~85 | ~20 | customer_id INT vs VARCHAR |
| crm.orders | RED | BLOCK(cluster_type_inconsistency) | ~85 | ~20 | customer_id VARCHAR vs INT |
| finance.mega_journal | YELLOW | WARN(column_count_outlier) | ~75 | ~20 | Extreme width |
| operations.wide_staging | YELLOW | WARN(column_count_outlier) | ~75 | ~20 | Moderate width |
| technical_debt.tbl_junk_7 | RED | INFO(null-rate skipped) | 0 | ~0 | No glossary/type support |
| operations.import_staging | YELLOW | WARN(high_null_rate) | ~80 | ~20 | Sample rows with >80% NULL |
