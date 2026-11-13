TEST (mode macro, name "commerce expressions preserve integer money and safe rates");

WITH expression_inputs AS (
  SELECT
    3::UInt32 AS quantity,
    1299::UInt32 AS unit_price_cents,
    0 AS cancelled,
    0 AS orders,
    'eu-west' AS region_code
),
__macro_actual__ AS (
  SELECT
    @line_revenue_cents("quantity", "unit_price_cents") AS line_revenue_cents,
    @safe_rate("cancelled", "orders") AS cancellation_rate,
    @region_name("region_code") AS region_name
  FROM expression_inputs
),
__macro_expected__ AS (
  SELECT 3897::UInt64 AS line_revenue_cents, 0.0 AS cancellation_rate, 'Europe West' AS region_name
)
SELECT 1
