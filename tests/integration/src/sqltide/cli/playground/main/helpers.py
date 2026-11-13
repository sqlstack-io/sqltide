def query_rows(output: str) -> tuple[tuple[int, str, int], ...]:
    """The `order_id, status, total_cents` rows of `sqt playground query` table output."""

    lines: list[str] = output.strip().splitlines()
    assert lines[0].split() == ["order_id", "status", "total_cents"]
    assert lines[-1].startswith("(")
    rows: list[list[str]] = [line.split() for line in lines[1:-1]]
    return tuple(
        (int(order_id), status, int(total_cents)) for order_id, status, total_cents in rows
    )
