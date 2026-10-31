"""Captured planner outputs for one corpus project."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlannerCapture:
    project: str
    scenarios: dict[str, object]
