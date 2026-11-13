AUDIT (
  name "orders_no_future_events",
  severity warning,
  every "10s",
  description "Warn when an order event is more than two seconds ahead of the ClickHouse clock.",
);

SELECT event_id, order_id, event_at
FROM __ref("order_events")
WHERE event_at > now64(3) + INTERVAL 2 SECOND
