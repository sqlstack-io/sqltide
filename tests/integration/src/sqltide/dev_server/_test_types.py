from dataclasses import dataclass


@dataclass(frozen=True)
class ActivityEvidenceTestCase:
    description: str
    relation_name: str
    expected_state: str
    expected_source: str
    expected_rows_written: int


@dataclass(frozen=True)
class ScheduledAuditWarehouseTestCase:
    description: str
    expected_first_tick_count: int
    expected_later_tick_count: int
    expected_status: str
    expected_outcome: str
    expected_event_kinds: tuple[str, ...]


@dataclass(frozen=True)
class ScheduledAuditOutcomeTestCase:
    description: str
    severity: str
    audit_query: str
    expected_status: str
    expected_outcome: str


@dataclass(frozen=True)
class MessageCorpusQueryTestCase:
    description: str
    request_json: dict
    expected_coordinates: tuple[tuple[int, int], ...]
    expected_window_seconds: int | None
    expected_has_next_cursor: bool


@dataclass(frozen=True)
class MessagePaginationTestCase:
    description: str
    page_limit: int
    expected_walk: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class MessageTruncationTestCase:
    description: str
    expected_preview_chars: int
    expected_duplicate_headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MessageFacetsCorpusTestCase:
    description: str
    expected_values: tuple[tuple[str, int], ...]
    expected_null_count: int
    expected_total_count: int
    expected_window_seconds: int


@dataclass(frozen=True)
class MessageSchemaResetTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class DeploymentRollbackRouteTestCase:
    description: str
    target_deployment_id: str
    expected_status: int
    expected_states: dict[str, str]
    expected_previous_deployment_id: str | None
    expected_error_message: str | None
    expected_rollback_run_count: int
