-- Version B's own view, queried directly before it is published.
SELECT
    sum(order_count) AS orders,
    sum(refund_count) AS refunds,
    round(sum(gross_revenue), 2) AS gross_revenue,
    round(sum(net_revenue), 2) AS net_revenue
FROM sqltide_streaming.view__commerce_kpis__20260101T110000Z_b;
