"""Recognize deployment-suffixed physical object names."""

import sqltide._native as _native


def is_deployment_physical_name(physical_name: str) -> bool:
    """Return whether the name is a SQLTide deployment-suffixed physical name."""

    return _native.is_deployment_physical_name(physical_name)
