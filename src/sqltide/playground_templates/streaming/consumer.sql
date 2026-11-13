-- What a dashboard reads: the stable view, whichever version is published.
SELECT
    sum(order_count) AS orders,
    sum(refund_count) AS refunds,
    round(sum(gross_revenue), 2) AS gross_revenue
FROM view__commerce_kpis;
