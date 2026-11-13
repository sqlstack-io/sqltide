MODEL (
  description "Validated order-created and order-status-change events.",
  order_by ["order_id", "event_at", "event_id", "_replay_offset"],
  partition_by "toYYYYMM(_replay_landed_at)",
  ttl "_replay_landed_at + INTERVAL 7 DAY",
  columns (
    event_id (description "Unique lifecycle event identifier."),
    order_id (description "Order identifier shared by all events in one lifecycle."),
    customer_id (description "Synthetic customer identifier."),
    quantity (description "Positive item quantity fixed when the order is created."),
    unit_price_cents (description "Unit price in integer minor currency units."),
    currency (description "ISO 4217 currency code.", audits [accepted_values (values ["USD"])]),
    status (
      description "Order lifecycle state represented by this event.",
      audits [
        accepted_values (
          values ["created", "paid", "shipped", "delivered", "cancelled", "refunded"],
        ),
      ],
    ),
    event_at (description "UTC business timestamp used for retention and daily metrics."),
  ),
  audits [
    expression_is_true (
      name "order quantities and prices are positive",
      expression "quantity > 0 AND unit_price_cents > 0",
    ),
  ],
);

SELECT
  event_id::String AS event_id,
  event_type::String AS event_type,
  schema_version::UInt16 AS schema_version,
  order_id::String AS order_id,
  customer_id::String AS customer_id,
  product::String AS product,
  category::String AS category,
  quantity::UInt32 AS quantity,
  unit_price_cents::UInt32 AS unit_price_cents,
    currency::String AS currency,
    status::String AS status,
    region_code::String AS region_code,
    @region_name("region_code")::String AS region_name,
  event_at::DateTime64(3) AS event_at,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset,
  _replay_timestamp::DateTime64(3) AS _replay_timestamp,
  _replay_landed_at::DateTime64(3) AS _replay_landed_at
FROM __ref("commerce_events")
