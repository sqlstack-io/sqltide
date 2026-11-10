"""Dev server result models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from sqltide._native import ControlStore
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.compiler.discovery.constants import DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS
from sqltide.compiler.pipeline.models import CompilationTimings, CompileAnalysis
from sqltide.dev_server.types import CompileStateKind
from sqltide.executor.destruction.classes.relational_destruction_plan_store import (
    RelationalDestructionPlanStore,
)


@dataclass(frozen=True)
class ReplayOffsetProgress:
    """Approximate progress derived from committed target offset frontiers."""

    percentage: float
    eta_seconds: float | None
    completed_span: int
    total_span: int
    observed_partitions: int
    total_partitions: int


@dataclass(frozen=True, repr=False)
class DevExecutionContext:
    """Resolved invocation values retained across dev reloads, previews, and builds."""

    database: str | None = None
    selected_target: str | None = None
    cli_variables: tuple[tuple[str, object], ...] = ()
    environment: Mapping[str, str] | None = None
    connection_host: str | None = None
    connection_port: int | None = None
    connection_username: str | None = None
    connection_password: str | None = None
    run_presumed_failed_after_seconds: int = DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS
    connection_factory: Callable[[], AdapterConnection] | None = None
    observation_connection_factory: Callable[[], AdapterConnection] | None = None


@dataclass(frozen=True)
class CompileErrorInfo:
    """A compile failure reduced to what an error page needs."""

    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None


@dataclass(frozen=True)
class CompileOutcome:
    """One held project compile: either servable definitions or the failure."""

    state: CompileStateKind
    version_key: str
    compiled_at: str
    analysis: CompileAnalysis | None = None
    timings: CompilationTimings | None = None
    error: CompileErrorInfo | None = None


@dataclass(frozen=True)
class OperationAuthorizationContext:
    """Shared control-plane inputs for route-level operation authorization."""

    store: ControlStore
    project_dir: Path
    selected_target: str | None


@dataclass(frozen=True)
class DevControlStores:
    """Durable control-plane stores and their application ownership."""

    accounts: ControlStore
    destruction_plans: RelationalDestructionPlanStore
    owns_accounts: bool

    def close(self) -> None:
        self.destruction_plans.close()
        if self.owns_accounts:
            self.accounts.close()


class ChecksRunRequest(BaseModel):
    """POST /api/checks/run body."""

    kind: str
    name: str


class AuditBatchRunRequest(BaseModel):
    """POST /api/audits/run body."""

    names: list[str] = Field(min_length=1)


class BuildRunRequest(BaseModel):
    """POST /api/build body."""

    selectors: list[str] = []
    startTime: str | None = None  # noqa: N815 - wire format is camelCase
    deploymentId: str | None = None  # noqa: N815 - wire format is camelCase
    confirmations: list[str] = []
    changed: bool = False
    includeMissingUpstream: bool = False  # noqa: N815 - wire format is camelCase


class MessageQueryMode(BaseModel):
    """One mutually exclusive message browsing window."""

    kind: str = "newest"
    fromTime: str | None = None  # noqa: N815 - wire format is camelCase
    toTime: str | None = None  # noqa: N815 - wire format is camelCase
    partition: int | None = None
    fromOffset: int | None = None  # noqa: N815 - wire format is camelCase
    toOffset: int | None = None  # noqa: N815 - wire format is camelCase


class MessageQueryPredicate(BaseModel):
    """One typed filter chip compiled to SQL server-side."""

    field: str
    op: str = "eq"
    value: str | int | float | None = None
    values: list[int] = []
    path: list[str | int] = []


class MessageQueryCursor(BaseModel):
    """Keyset pagination position after the last returned row."""

    landedAt: str  # noqa: N815 - wire format is camelCase
    partition: int
    offset: int


class MessagesQueryRequest(BaseModel):
    """POST /api/sources/{name}/messages body."""

    mode: MessageQueryMode = MessageQueryMode()
    predicates: list[MessageQueryPredicate] = []
    limit: int = 50
    cursor: MessageQueryCursor | None = None
    timeColumn: str = "landed"  # noqa: N815 - wire format is camelCase
    previewPaths: list[list[str | int]] = []  # noqa: N815 - wire format is camelCase


class MessageRecordRequest(BaseModel):
    """POST /api/sources/{name}/messages/record body."""

    partition: int
    offset: int


class MessageFacetsRequest(BaseModel):
    """POST /api/sources/{name}/messages/facets body."""

    mode: MessageQueryMode = MessageQueryMode()
    predicates: list[MessageQueryPredicate] = []
    limit: int = 50
    timeColumn: str = "landed"  # noqa: N815 - wire format is camelCase
    facetPath: list[str | int] = []  # noqa: N815 - wire format is camelCase


@dataclass(frozen=True)
class KafkaTopicInfo:
    """Broker metadata for one topic."""

    name: str
    partition_count: int
    replication_factor: int
    internal: bool


@dataclass(frozen=True)
class KafkaTopicsSnapshot:
    """Best-effort topic inventory for one broker list."""

    topics: tuple[KafkaTopicInfo, ...]


@dataclass(frozen=True)
class KafkaPartitionLag:
    """Broker offsets and lag for one topic partition."""

    partition: int
    committed_offset: int | None
    end_offset: int
    lag_messages: int | None


@dataclass(frozen=True)
class KafkaLagSnapshot:
    """Best-effort consumer-group lag for one managed Kafka source."""

    total_messages: int | None
    partitions: tuple[KafkaPartitionLag, ...]


@dataclass(frozen=True)
class RelationStorage:
    """Row and byte totals for one warehouse relation."""

    rows: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class BuildCancellationOutcome:
    """Terminal evidence from one server-owned build cancellation attempt."""

    invocation_id: str
    query_id: str | None
    requested_at: str
    status: Literal["cancelled", "cancellation_failed"]
    process_exit_code: int
    warehouse_supported: bool | None
    query_found: bool | None
    warehouse_termination_confirmed: bool | None
    error_message: str | None = None


class DeploymentCleanupRequest(BaseModel):
    """Janitor apply request from the development UI."""

    retentionDays: int = 7


class DestructionPlanRequest(BaseModel):
    """Create one immutable destructive-operation impact plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    operation: Literal["destroy_pipelines", "reset_target"]
    pipelineNames: list[str] = Field(default_factory=list)  # noqa: N815 - wire format
    includedDependentPipelineNames: list[str] = Field(  # noqa: N815 - wire format
        default_factory=list
    )


class DestructionExecutionRequest(BaseModel):
    """Exact manually entered responses for one reviewed frozen plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    responses: list[str]


class SensorStatusRequest(BaseModel):
    """POST /api/sensors/{name}/status body."""

    status: str


class SensorDeadLetterActionRequest(BaseModel):
    """POST /api/sensors/dead-letters/{retry,skip} body."""

    sensorName: str
    eventId: str
    reason: str | None = None
