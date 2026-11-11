"""Apache-2.0: SQLBuild providers.py@7625d22e2716."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

import sqltide._native as _native


class Provider(BaseSettings):
    """Base class for SQLTide runtime providers."""

    provider_name: ClassVar[str | None] = None

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="forbid", arbitrary_types_allowed=True
    )

    @classmethod
    def name(cls) -> str:
        """Return the runtime provider name for this provider class."""

        return _native.provider_name(cls)

    def setup(self, ctx: Any) -> None:
        """Prepare the provider for runtime use."""

    def teardown(self) -> None:
        """Release runtime resources held by the provider."""
