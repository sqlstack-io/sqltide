"""Small SQL expression macros used by the commerce models and tests."""


def line_revenue_cents(quantity_column: str, unit_price_column: str) -> str:
    """Return an overflow-safe integer expression for one created order."""

    return f"toUInt64({quantity_column}) * toUInt64({unit_price_column})"


def safe_rate(numerator_column: str, denominator_column: str) -> str:
    """Return a Float64 rate that is zero when its denominator is zero."""

    return (
        f"if({denominator_column} = 0, 0.0, "
        f"toFloat64({numerator_column}) / toFloat64({denominator_column}))"
    )


def region_name(region_column: str) -> str:
    """Map stable demo region codes without a mutable side table."""

    return (
        f"multiIf({region_column} = 'us-east', 'US East', "
        f"{region_column} = 'us-west', 'US West', "
        f"{region_column} = 'eu-west', 'Europe West', "
        f"{region_column} = 'ap-south', 'Asia Pacific South', {region_column})"
    )
