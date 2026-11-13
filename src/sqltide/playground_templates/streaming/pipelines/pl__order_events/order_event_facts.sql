MODEL (
  description "Append-only additive facts; terminal consumers deduplicate by stable event id.",
  order_by ["event_id", "_replay_partition", "_replay_offset"],
  partition_by "toYYYYMM(_replay_landed_at)",
  ttl "_replay_landed_at + INTERVAL 7 DAY",
  columns (
    event_id (description "Stable lifecycle event id used by terminal deduplication."),
    event_count (description "Additive count of lifecycle events; always one per row."),
    order_count (description "Additive count of order-created events."),
    cancellation_count (description "Additive count of cancelled events."),
    refund_count (description "Additive count of refunded events."),
    units_ordered (description "Additive units from order-created events only."),
    gross_revenue_cents (description "Additive created-order value in integer cents."),
  ),
);

SELECT
  event_id::String AS event_id,
  order_id::String AS order_id,
  category::String AS category,
  currency::String AS currency,
  region_code::String AS region_code,
  region_name::String AS region_name,
  event_at::DateTime64(3) AS event_at,
  toDate(event_at)::Date AS event_day,
  1::UInt64 AS event_count,
  toUInt64(status = 'created')::UInt64 AS order_count,
  toUInt64(status = 'cancelled')::UInt64 AS cancellation_count,
  toUInt64(status = 'refunded')::UInt64 AS refund_count,
  if(status = 'created', toUInt64(quantity), 0)::UInt64 AS units_ordered,
  if(status = 'created', @line_revenue_cents("quantity", "unit_price_cents"), 0)::UInt64 AS gross_revenue_cents,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset,
  _replay_timestamp::DateTime64(3) AS _replay_timestamp,
  _replay_landed_at::DateTime64(3) AS _replay_landed_at
FROM __ref("order_events")
