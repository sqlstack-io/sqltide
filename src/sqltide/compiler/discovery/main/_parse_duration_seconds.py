"""Parse compact authored durations into typed seconds."""

import sqltide._native as _native


def parse_duration_seconds(*, value: object, field_path: str, allow_zero: bool) -> int:
    """Parse one `<int><s|m|h|d>` duration with explicit zero semantics."""

    return _native.parse_duration_seconds(value, field_path, allow_zero)
