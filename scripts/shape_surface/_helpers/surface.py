"""Compare the installed shape surface with the recorded baseline."""

from __future__ import annotations

import importlib
import json
from types import ModuleType

from scripts.shape_surface._helpers.inventory import describe_value, module_statements


def surface_errors(
    *,
    baseline: dict[str, dict[str, object]],
    removed: dict[str, str],
    volatile: frozenset[str],
) -> list[str]:
    """Each recorded name that is missing, changed, or removed without being listed."""
    errors: list[str] = []
    for key, recorded in baseline.items():
        module_name, name = key.split(":", 1)
        try:
            module: ModuleType = importlib.import_module(module_name)
            value: object = getattr(module, name)
        except (ImportError, AttributeError):
            if key not in removed:
                errors.append(
                    f"{key} is no longer importable; restore it, or list it in REMOVED with "
                    "the reason it is safe to drop."
                )
            continue
        if key in removed:
            errors.append(f"{key} is listed in REMOVED but still exists; drop the REMOVED entry.")
            continue
        probe: object = recorded.get("probe")
        current: dict[str, object] = describe_value(
            value=value,
            alias_source=module_statements(module)[1].get(name, ""),
            probe={str(field): item for field, item in probe.items()}
            if isinstance(probe, dict)
            else None,
        )
        if key in volatile:
            current = {**current, "value": recorded.get("value")}
        errors.extend(
            f"{key} {field} changed: was {json.dumps(recorded.get(field))}, "
            f"now {json.dumps(current.get(field))}"
            for field in sorted(set(recorded) | set(current))
            if recorded.get(field) != current.get(field)
        )
    errors.extend(
        f"REMOVED entry {key} is not in the baseline; drop it."
        for key in sorted(set(removed) - set(baseline))
    )
    return errors
