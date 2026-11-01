"""Runtime models for authored Python SQL macros."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import sqltide._native as _native
from sqltide.compiler.discovery.main._immutable_config_pairs import immutable_config_pairs
from sqltide.compiler.discovery.types import PipelineMode
from sqltide.compiler.macros.types import MacroFunction


@dataclass(frozen=True)
class LoadedMacro:
    """One registered authored Python macro."""

    name: str
    file_path: Path
    relative_path: Path
    source: str
    definition_line: int
    function: MacroFunction
    description: str | None = None

    @property
    def identity_fingerprint(self) -> str:
        """Fingerprint the macro identity and retained implementation source."""

        payload: str = f"{self.name}\0{self.relative_path.as_posix()}\0{self.source}"
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class MacroRegistry:
    """Immutable macro definitions and invocation-local expansion source maps."""

    macros: Mapping[str, LoadedMacro] = field(default_factory=dict)
    declarations: _native.Declarations | None = None
    expansions: dict[tuple[Path, str, int, int], tuple[str, list[tuple[int, int, int, int]]]] = (
        field(default_factory=dict, compare=False, repr=False)
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "macros", MappingProxyType(dict(self.macros)))


@dataclass(frozen=True)
class PipelineNamingContext:
    """Immutable authored pipeline metadata supplied to a naming macro."""

    name: str
    source_name: str | None
    model_names: tuple[str, ...]
    relative_directory: str
    mode: PipelineMode


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
        frozen_variables: tuple[tuple[str, object], ...] = immutable_config_pairs(
            tuple(self.variables.items())
        )
        object.__setattr__(self, "variables", MappingProxyType(dict(frozen_variables)))
