from dataclasses import dataclass


@dataclass(frozen=True)
class PendingPublishTestCase:
    description: str
    expected_send_count: int
    expected_flush_count: int


@dataclass(frozen=True)
class CrashReplayTestCase:
    description: str
    expected_crash_send_count: int
    expected_restart_send_count: int


@dataclass(frozen=True)
class RandomContinuationTestCase:
    description: str
    expected_order_id: str
