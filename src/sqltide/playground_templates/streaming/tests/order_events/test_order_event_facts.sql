TEST (name "order facts emit additive values");

WITH __ref__order_events AS (
  SELECT
    'evt_created' AS event_id,
    'ord_001' AS order_id,
    'electronics' AS category,
    'USD' AS currency,
    'us-east' AS region_code,
    'US East' AS region_name,
    'created' AS status,
    2::UInt32 AS quantity,
    1250::UInt32 AS unit_price_cents,
    toDateTime64('2026-08-23 10:00:00', 3) AS event_at,
    0 AS _replay_partition,
    1 AS _replay_offset,
    toDateTime64('2026-08-23 10:00:00', 3) AS _replay_timestamp,
    toDateTime64('2026-08-23 10:00:00', 3) AS _replay_landed_at
  UNION ALL
  SELECT
    'evt_cancelled', 'ord_001', 'electronics', 'USD', 'us-east', 'US East',
    'cancelled', 2::UInt32, 1250::UInt32,
    toDateTime64('2026-08-23 10:05:00', 3), 0, 2,
    toDateTime64('2026-08-23 10:05:00', 3),
    toDateTime64('2026-08-23 10:05:00', 3)
),
__expected__order_event_facts AS (
  SELECT
    'evt_created' AS event_id,
    1::UInt64 AS event_count,
    1::UInt64 AS order_count,
    0::UInt64 AS cancellation_count,
    0::UInt64 AS refund_count,
    2::UInt64 AS units_ordered,
    2500::UInt64 AS gross_revenue_cents
  UNION ALL
  SELECT 'evt_cancelled', 1::UInt64, 0::UInt64, 1::UInt64, 0::UInt64, 0::UInt64, 0::UInt64
)
SELECT 1
