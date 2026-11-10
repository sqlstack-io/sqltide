"""Test-case models for operational authorization."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqltide._native import ControlStore
from sqltide.authorization.models import AuthorizationRequest
from sqltide.authorization.types import AuthorizationReason

type AuthorizationScenario = tuple[ControlStore, AuthorizationRequest]


class AuthorizationScenarioBuilder(Protocol):
    """Build one ready-to-evaluate operation and its store."""

    def __call__(self, *, tmp_path: Path) -> AuthorizationScenario: ...


@dataclass(frozen=True)
class AuthorizationTestCase:
    """Expected result for one authorization scenario builder."""

    description: str
    build_scenario: AuthorizationScenarioBuilder
    expected_allowed: bool
    expected_reason: AuthorizationReason
    expected_roles: tuple[str, ...] = ()
    expected_missing_pipelines: tuple[str, ...] = ()
