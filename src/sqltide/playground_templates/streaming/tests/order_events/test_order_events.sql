TEST (name "order events derive stable region labels");

WITH __ref__commerce_events AS (
  SELECT
    'evt_001' AS event_id,
    'order_created' AS event_type,
    1::UInt16 AS schema_version,
    'ord_001' AS order_id,
    'cust_001' AS customer_id,
    'Northstar USB-C Dock' AS product,
    'electronics' AS category,
    2::UInt32 AS quantity,
    12900::UInt32 AS unit_price_cents,
    'USD' AS currency,
    'created' AS status,
    'us-east' AS region_code,
    toDateTime64('2026-08-23 10:00:00', 3) AS event_at,
    0 AS _replay_partition,
    1 AS _replay_offset,
    toDateTime64('2026-08-23 10:00:00', 3) AS _replay_timestamp,
    toDateTime64('2026-08-23 10:00:00', 3) AS _replay_landed_at
),
__expected__order_events AS (
  SELECT 'evt_001' AS event_id, 'ord_001' AS order_id, 'US East' AS region_name
)
SELECT 1
