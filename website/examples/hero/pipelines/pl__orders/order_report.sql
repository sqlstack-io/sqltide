MODEL (order_by [order_id]);

SELECT
  CAST(order_id AS String) AS order_id,
  CAST(amount_cents AS UInt64) AS amount_cents,
  CAST(_replay_partition AS Int32) AS _replay_partition,
  CAST(_replay_offset AS Int64) AS _replay_offset,
  CAST(_replay_timestamp AS DateTime64(3)) AS _replay_timestamp
FROM __ref("order_totals")
