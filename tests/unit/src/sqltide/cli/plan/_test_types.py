from dataclasses import dataclass


@dataclass(frozen=True)
class RowErrorPolicyLineTestCase:
    description: str
    landing_repair_only: bool
    expected_line: str
