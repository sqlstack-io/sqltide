"""Parse SQLBuild-style MODEL header values."""

from __future__ import annotations

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import ModelHeaderSyntaxError


def parse_model_header(*, header: str) -> dict[str, object]:
    """Parse one MODEL header with SQLBuild's map and list grammar."""

    try:
        return _native.parse_model_header(header)
    except ValueError as error:
        raise ModelHeaderSyntaxError(str(error)) from None
