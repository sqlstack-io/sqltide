from dataclasses import dataclass

from sqltide.compiler.discovery.types import RowErrorPolicy


@dataclass(frozen=True)
class PolicyViewStepsTestCase:
    description: str
    desired_policy: RowErrorPolicy
    renderable_views: tuple[str, ...]
    expected_realized_steps: tuple[str, ...]
