from collections.abc import Callable
from dataclasses import dataclass

from sqltide.executor.destruction.exceptions import (
    DestructionChallengeError,
    DestructionPlanNotFoundError,
)
from sqltide.executor.destruction.types import DestructionOwnership
from tests.unit.src.sqltide.executor.destruction.helpers import PlanningFixture


@dataclass(frozen=True)
class DestructionChallengeTestCase:
    description: str
    pipeline_names: tuple[str, ...]
    production_reset: bool
    expected_challenges: tuple[str, ...]


@dataclass(frozen=True)
class DestructionDependencyTestCase:
    description: str
    fixture_builder: Callable[[], PlanningFixture]
    expected_dependent_pipeline_names: tuple[str, ...]


@dataclass(frozen=True)
class PreservedSourceClosureTestCase:
    description: str
    expected_affected_pipeline_names: tuple[str, ...]


@dataclass(frozen=True)
class WarehouseDependencyFailureTestCase:
    description: str
    expected_error_value: str


@dataclass(frozen=True)
class DestructionDropLimitTestCase:
    description: str
    limit: int
    expected_resource_fragments: tuple[str, ...]


@dataclass(frozen=True)
class DestructionDropOverrideTestCase:
    description: str
    server_limit: int
    override_limit: int
    expected_effective_limit: int


@dataclass(frozen=True)
class DestructionFrozenDropLimitTestCase:
    description: str
    first_limit: int
    second_limit: int
    expected_fingerprint_changed: bool


@dataclass(frozen=True)
class DestructionClosureTestCase:
    description: str
    expected_requested_pipeline_names: tuple[str, ...]
    expected_included_pipeline_names: tuple[str, ...]
    expected_affected_pipeline_names: tuple[str, ...]
    expected_affected_model_names: tuple[str, ...]


@dataclass(frozen=True)
class PipelineDestructionPlanTestCase:
    description: str
    expected_relation_names: tuple[str, ...]
    expected_excluded_relation_names: tuple[str, ...]
    expected_affected_source_names: tuple[str, ...]
    expected_preserves_sources: bool
    expected_preserves_replay_data: bool
    expected_stable_ownership: tuple[DestructionOwnership, ...]
    expected_total_bytes: int
    expected_active_parts: int
    expected_catalog_databases: list[str]
    expected_inventory_databases: list[str]
    expected_query_count: int


@dataclass(frozen=True)
class TargetResetPlanTestCase:
    description: str
    expected_included_relation_names: tuple[str, ...]
    expected_excluded_relation_names: tuple[str, ...]
    expected_affected_source_names: tuple[str, ...]
    expected_preserves_sources: bool
    expected_preserves_replay_data: bool
    expected_challenges: tuple[str, ...]


@dataclass(frozen=True)
class StableFingerprintTestCase:
    description: str
    expected_fingerprint_length: int


@dataclass(frozen=True)
class StableDriftFingerprintTestCase:
    description: str
    changed_stats: tuple[tuple[str, int, int], ...]
    expected_estimated_bytes_changed: bool


@dataclass(frozen=True)
class DuplicateSelectionTestCase:
    description: str
    expected_error_match: str


@dataclass(frozen=True)
class PlanningBehaviorTestCase:
    description: str
    expected_value: str


@dataclass(frozen=True)
class OrphanManifestTestCase:
    description: str
    expected_included_relation: str
    expected_excluded_relations: frozenset[str]


@dataclass(frozen=True)
class InactivePipelineDestructionTestCase:
    description: str
    expected_pipeline_name: str
    expected_logical_name: str
    expected_manifest_relation_name: str
    expected_physical_relation_name: str
    expected_last_published_at: str


@dataclass(frozen=True)
class UnsupportedManifestVersionTestCase:
    description: str
    manifest_version: int
    expected_error_match: str


@dataclass(frozen=True)
class VirtualHistoryResetTestCase:
    description: str
    expected_reset_relation_names: tuple[str, ...]
    expected_destroy_excluded_names: tuple[str, ...]


@dataclass(frozen=True)
class StaleLedgerImpactTestCase:
    description: str
    expected_pipeline_names: tuple[str, ...]
    expected_model_names: tuple[str, ...]
    expected_source_names: tuple[str, ...]
    expected_challenges: tuple[str, ...]


@dataclass(frozen=True)
class StoreErrorTestCase:
    description: str
    expected_error: type[Exception]


@dataclass(frozen=True)
class StoreConsumeTwiceTestCase:
    description: str
    expected_second_error: type[Exception]


@dataclass(frozen=True)
class ConcurrentConsumeTestCase:
    description: str
    expected_outcomes: tuple[str, ...]


@dataclass(frozen=True)
class StoreExpiryTestCase:
    description: str
    expected_expiry_error: type[Exception]
    expected_removed_error: type[Exception]


@dataclass(frozen=True)
class DurableStoreTestCase:
    description: str
    actor: str = "alice"
    other_actor: str = "bob"
    expected_status: str = "consumed"
    expected_dependency_names: tuple[str, ...] = ("analytics.upstream",)
    expected_error: type[Exception] = DestructionPlanNotFoundError
    expected_challenge_error: type[Exception] = DestructionChallengeError


@dataclass(frozen=True)
class LegacyDestructionPlanTestCase:
    description: str
    expected_policy_observed: bool
    expected_limit: int | None


@dataclass(frozen=True)
class CorruptTimestampTestCase:
    description: str
    created_at: str
    expires_at: str
    expected_message: str


@dataclass(frozen=True)
class DestructionWorkflowTestCase:
    description: str
    expected_first_sql: str
    expected_sql_suffix: str
    expected_view_sql: str
    expected_table_sql: str
    expected_has_relation_kinds: bool


@dataclass(frozen=True)
class DeterministicDestructionOrderTestCase:
    description: str
    expected_orders_equal: bool


@dataclass(frozen=True)
class TombstoneAdjacencyTestCase:
    description: str
    expected_statement_multiplier: int


@dataclass(frozen=True)
class EquivalentDriftTestCase:
    description: str
    expected_plan_id: str


@dataclass(frozen=True)
class RejectedDriftTestCase:
    description: str
    expected_error_match: str


@dataclass(frozen=True)
class DestructionExecutionTestCase:
    description: str
    expected_outcome: str | None = None
    expected_completed_sequences: tuple[int, ...] = ()
    expected_pending_sequences: tuple[int, ...] = ()
    expected_remaining_names: tuple[str, ...] | None = ()
    expected_residual_status: str = "observed"
    expected_failure_phase: str | None = None
    expected_error_match: str | None = None
    expected_terminal_attempt_count: int = 1
