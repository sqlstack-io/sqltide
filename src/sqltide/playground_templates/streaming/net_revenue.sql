-- Net revenue exists only in version B: gross revenue minus refunds.
SELECT
    sum(order_count) AS orders,
    round(sum(gross_revenue), 2) AS gross_revenue,
    round(sum(net_revenue), 2) AS net_revenue
FROM view__commerce_kpis;
