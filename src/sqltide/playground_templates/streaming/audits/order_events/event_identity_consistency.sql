AUDIT (
  description "One event id must never identify conflicting commerce event bodies.",
);

SELECT event_id, count() AS physical_rows
FROM __ref("commerce_events")
GROUP BY event_id
HAVING uniqExact(tuple(
  event_type,
  schema_version,
  order_id,
  customer_id,
  product,
  category,
  quantity,
  unit_price_cents,
  currency,
  status,
  region_code,
  event_at
)) > 1
