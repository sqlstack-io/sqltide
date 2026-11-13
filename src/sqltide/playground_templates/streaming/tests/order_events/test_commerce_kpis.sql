TEST (name "terminal kpis deduplicate facts before deriving rates");

WITH __ref__order_event_facts AS (
  SELECT
    'evt_created' AS event_id,
    toDate('2026-08-23') AS event_day,
    'USD' AS currency,
    'us-east' AS region_code,
    'US East' AS region_name,
    'electronics' AS category,
    1::UInt64 AS event_count,
    1::UInt64 AS order_count,
    0::UInt64 AS cancellation_count,
    0::UInt64 AS refund_count,
    2::UInt64 AS units_ordered,
    2500::UInt64 AS gross_revenue_cents,
    1::Int64 AS _replay_offset
  UNION ALL
  SELECT
    'evt_created', toDate('2026-08-23'), 'USD', 'us-east', 'US East', 'electronics',
    1::UInt64, 1::UInt64, 0::UInt64, 0::UInt64, 2::UInt64, 2500::UInt64, 2::Int64
  UNION ALL
  SELECT
    'evt_cancelled', toDate('2026-08-23'), 'USD', 'us-east', 'US East', 'electronics',
    1::UInt64, 0::UInt64, 1::UInt64, 0::UInt64, 0::UInt64, 0::UInt64, 3::Int64
),
__expected__commerce_kpis AS (
  SELECT
    toDate('2026-08-23') AS event_day,
    2::UInt64 AS event_count,
    1::UInt64 AS order_count,
    1::UInt64 AS cancellation_count,
    2::UInt64 AS units_ordered,
    2500::UInt64 AS gross_revenue_cents,
    25.0 AS gross_revenue,
    25.0 AS average_order_value,
    1.0 AS cancellation_rate
)
SELECT 1
