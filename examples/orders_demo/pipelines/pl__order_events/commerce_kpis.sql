MODEL (
  kind view,
  description "Terminal KPI view that deduplicates event facts before deriving daily metrics.",
  columns (
    event_day (description "UTC calendar day represented by the KPI row."),
    gross_revenue (description "Gross created-order value in major currency units."),
    average_order_value (description "Gross value divided by created orders, or zero without orders."),
    cancellation_rate (description "Cancelled events divided by created orders, or zero without orders."),
    refund_rate (description "Refunded events divided by created orders, or zero without orders."),
  ),
);

WITH latest_event_facts AS (
  SELECT
    event_id,
    argMax(event_day, _replay_offset) AS event_day,
    argMax(currency, _replay_offset) AS currency,
    argMax(region_code, _replay_offset) AS region_code,
    argMax(region_name, _replay_offset) AS region_name,
    argMax(category, _replay_offset) AS category,
    argMax(event_count, _replay_offset) AS event_count,
    argMax(order_count, _replay_offset) AS order_count,
    argMax(cancellation_count, _replay_offset) AS cancellation_count,
    argMax(refund_count, _replay_offset) AS refund_count,
    argMax(units_ordered, _replay_offset) AS units_ordered,
    argMax(gross_revenue_cents, _replay_offset) AS gross_revenue_cents
  FROM __ref("order_event_facts")
  GROUP BY event_id
),
daily_metrics AS (
  SELECT
    event_day,
    currency,
    region_code,
    region_name,
    category,
    sum(event_count)::UInt64 AS event_count,
    sum(order_count)::UInt64 AS order_count,
    sum(cancellation_count)::UInt64 AS cancellation_count,
    sum(refund_count)::UInt64 AS refund_count,
    sum(units_ordered)::UInt64 AS units_ordered,
    sum(gross_revenue_cents)::UInt64 AS gross_revenue_cents
  FROM latest_event_facts
  GROUP BY event_day, currency, region_code, region_name, category
)
SELECT
  event_day::Date AS event_day,
  currency::String AS currency,
  region_code::String AS region_code,
  region_name::String AS region_name,
  category::String AS category,
  event_count::UInt64 AS event_count,
  order_count::UInt64 AS order_count,
  cancellation_count::UInt64 AS cancellation_count,
  refund_count::UInt64 AS refund_count,
  units_ordered::UInt64 AS units_ordered,
  gross_revenue_cents::UInt64 AS gross_revenue_cents,
  (gross_revenue_cents / 100.0)::Float64 AS gross_revenue,
  (@safe_rate("gross_revenue_cents", "order_count") / 100.0)::Float64 AS average_order_value,
  @safe_rate("cancellation_count", "order_count")::Float64 AS cancellation_rate,
  @safe_rate("refund_count", "order_count")::Float64 AS refund_rate
FROM daily_metrics
