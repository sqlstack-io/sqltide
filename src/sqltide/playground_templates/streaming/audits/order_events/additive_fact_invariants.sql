AUDIT (
  description "Additive flags and money must agree for every lifecycle event fact.",
);

SELECT event_id, event_count, order_count, cancellation_count, refund_count, gross_revenue_cents
FROM __ref("order_event_facts")
WHERE event_count != 1
   OR order_count + cancellation_count + refund_count > event_count
   OR (order_count = 0 AND gross_revenue_cents != 0)
   OR (order_count = 1 AND gross_revenue_cents = 0)
