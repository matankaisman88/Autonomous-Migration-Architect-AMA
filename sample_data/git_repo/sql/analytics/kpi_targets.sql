-- KPI pack: quarterly targets (report consumers also slice by sales.orders in BI)
SELECT team, goal, quarter
FROM analytics.targets
WHERE quarter = EXTRACT(QUARTER FROM CURRENT_DATE)::int;

-- Reference: downstream dashboards join targets to sales.orders for attainment.
