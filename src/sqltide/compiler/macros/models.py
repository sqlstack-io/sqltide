"""Runtime models for authored Python SQL macros."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import sqltide._native as _native
from sqltide._native import LoadedMacro as LoadedMacro
from sqltide._native import MacroRegistry as MacroRegistry
from sqltide._native import PipelineNamingContext as PipelineNamingContext


@dataclass(frozen=True, repr=False)
class MacroContext:
    """Connection-free immutable context optionally injected into macros."""

    adapter_name: str
    dialect: str
    target_name: str | None
    database: str | None
    schema: str | None
    virtual_environments: bool
    variables: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen_variables: tuple[tuple[str, object], ...] = _native.immutable_config_pairs(
            tuple(self.variables.items())
        )
        object.__setattr__(self, "variables", MappingProxyType(dict(frozen_variables)))
