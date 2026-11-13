MODEL (
  description "Typed order lifecycle envelope parsed from the retained Kafka source.",
  order_by ["event_id", "_replay_partition", "_replay_offset"],
  partition_by "toYYYYMM(_replay_landed_at)",
  ttl "_replay_landed_at + INTERVAL 7 DAY",
  columns (
    event_id (description "Producer-assigned idempotency key for one commerce event."),
    event_type (
      description "Envelope discriminator for created orders and later lifecycle changes.",
      audits [
        accepted_values (
          values ["order_created", "order_status_changed"],
        ),
      ],
    ),
    schema_version (description "Version of the producer event contract."),
    order_id (description "Order identifier shared by every event in one lifecycle."),
    quantity (description "Native integer item quantity carried by the order lifecycle."),
    unit_price_cents (description "Native integer unit price in the event currency."),
    currency (description "ISO 4217 currency code."),
    region_code (description "Stable code used to enrich events from region definitions."),
    event_at (description "UTC business timestamp assigned by the producer."),
  ),
  audits [
    expression_is_true (
      name "event and order ids are non-empty",
      expression "length(event_id) > 0 AND length(order_id) > 0",
    ),
  ],
);

SELECT
  JSONExtractString(kafka_value, 'event_id')::String AS event_id,
  JSONExtractString(kafka_value, 'event_type')::String AS event_type,
  toUInt16(JSONExtractUInt(kafka_value, 'schema_version'))::UInt16 AS schema_version,
  JSONExtractString(kafka_value, 'order_id')::String AS order_id,
  JSONExtractString(kafka_value, 'customer_id')::String AS customer_id,
  JSONExtractString(kafka_value, 'product')::String AS product,
  JSONExtractString(kafka_value, 'category')::String AS category,
  toUInt32(JSONExtractUInt(kafka_value, 'quantity'))::UInt32 AS quantity,
  toUInt32(JSONExtractUInt(kafka_value, 'unit_price_cents'))::UInt32 AS unit_price_cents,
  JSONExtractString(kafka_value, 'currency')::String AS currency,
  JSONExtractString(kafka_value, 'status')::String AS status,
  JSONExtractString(kafka_value, 'region_code')::String AS region_code,
  parseDateTime64BestEffort(JSONExtractString(kafka_value, 'event_at'), 3)::DateTime64(3) AS event_at,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset,
  _replay_timestamp::DateTime64(3) AS _replay_timestamp,
  _replay_landed_at::DateTime64(3) AS _replay_landed_at
FROM __source("commerce_event_stream")
