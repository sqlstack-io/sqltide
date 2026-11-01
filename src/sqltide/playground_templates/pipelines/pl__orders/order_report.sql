MODEL (
  engine "MergeTree()",
  order_by ["order_id"]
);

SELECT
  order_id::UInt64 AS order_id,
  status::String AS status,
  total_cents::Int64 AS total_cents,
  _replay_partition::Int32 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset
FROM __source("orders")
