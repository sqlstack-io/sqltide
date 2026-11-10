from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqltide.dev_server.models import KafkaLagSnapshot
from sqltide.dev_server.types import KafkaLagOffsets


@dataclass(frozen=True)
class BuildCancellationStateTestCase:
    description: str
    invocation_id: str
    expected_cancel_status: str
    expected_force_available: bool


@dataclass(frozen=True)
class BuildWarehouseCancellationTestCase:
    description: str
    invocation_id: str
    query_id: str
    termination_confirmed: bool
    cancellation_detail: str | None
    expected_terminal_status: str
    expected_error: str | None


@dataclass(frozen=True)
class BuildCancellationPersistenceTestCase:
    description: str
    invocation_id: str
    query_id: str
    outcome_status: Literal["cancelled", "cancellation_failed"]
    termination_confirmed: bool
    error_message: str | None
    expected_event_kind: str
    expected_sequence: int


@dataclass(frozen=True)
class BuildStartTestCase:
    description: str
    selector: str
    expected_status: str
    expected_running: bool


@dataclass(frozen=True)
class KafkaLagSnapshotTestCase:
    description: str
    partition_ids: frozenset[int]
    committed_offsets: tuple[tuple[int, int], ...]
    end_offsets: tuple[tuple[int, int], ...]
    expected_total_messages: int | None
    expected_partition_lags: tuple[int | None, ...]


@dataclass(frozen=True)
class AuditScheduleCalculationTestCase:
    description: str
    status_payloads: tuple[dict[str, object], ...]
    anchors_by_model: dict[str, str]
    warmup_anchor: str | None
    eligible_at: str | None
    warmup_eligible: bool
    materialization_outcome: str | None
    expected_scheduler_state: str
    expected_state: str
    expected_scheduled_for: str | None
    expected_missing_relations: tuple[str, ...] = ()
    warehouse_now: str = "2026-08-08 12:00:00.000"


@dataclass(frozen=True)
class AuditSchedulerBackoffTestCase:
    description: str
    error_message: str
    expected_initial_backoff_seconds: float
    expected_result_count_after_recovery: int


@dataclass(frozen=True)
class AuditSchedulerActiveRunTestCase:
    description: str
    active_runs: tuple[dict[str, object], ...]
    latest_applied_at: str | None
    expected_payload_reads: int
    expected_latest_error: str | None


@dataclass(frozen=True)
class AuditSchedulerLocalRaceTestCase:
    description: str
    expected_feed_reads: int


@dataclass(frozen=True)
class KafkaTopicsSnapshotTestCase:
    description: str
    metadata: tuple[dict, ...]
    expected_topics: tuple[tuple[str, int, int, bool], ...]


@dataclass(frozen=True)
class KafkaClientReuseTestCase:
    description: str
    broker_list: str
    expected_client_count: int
    expected_query_count: int
    expected_close_count: int


@dataclass(frozen=True)
class KafkaLagReaderCacheTestCase:
    description: str
    respond: Callable[[], KafkaLagOffsets]
    expected_snapshot: KafkaLagSnapshot | None
    expected_collector_calls: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class KafkaCredentialIsolationTestCase:
    description: str
    first_username: str
    second_username: str
    expected_isolated: bool


@dataclass(frozen=True)
class WarehouseRuntimeRecoveryTestCase:
    description: str
    failure_message: str
    expected_attempts: int
    expected_state: str


@dataclass(frozen=True)
class WarehouseHealthRetentionTestCase:
    description: str
    expected_availability: str
    expected_status: str
    expected_stale: bool
    expected_warning: str
    first_measured_at: str
    second_measured_at: str
    second_database: str
    expected_disk_count: int
    expected_measured_at: str


@dataclass(frozen=True)
class WarehouseHealthCacheTestCase:
    description: str
    clock_values: tuple[float, ...]
    expected_provider_reads: int
    expected_second_measured_at: str
