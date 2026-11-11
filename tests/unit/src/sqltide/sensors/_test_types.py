from dataclasses import dataclass


@dataclass(frozen=True)
class SensorRetryPolicyErrorTestCase:
    description: str
    max_attempts: int
    backoff_seconds: float
    expected_message: str
