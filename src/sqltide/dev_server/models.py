"""Dev server request bodies and result models."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from sqltide._native import BuildCancellationOutcome as BuildCancellationOutcome
from sqltide._native import CompileErrorInfo as CompileErrorInfo
from sqltide._native import CompileOutcome as CompileOutcome
from sqltide._native import DevControlStores as DevControlStores
from sqltide._native import DevExecutionContext as DevExecutionContext
from sqltide._native import KafkaLagSnapshot as KafkaLagSnapshot
from sqltide._native import KafkaPartitionLag as KafkaPartitionLag
from sqltide._native import KafkaTopicInfo as KafkaTopicInfo
from sqltide._native import KafkaTopicsSnapshot as KafkaTopicsSnapshot
from sqltide._native import OperationAuthorizationContext as OperationAuthorizationContext
from sqltide._native import RelationStorage as RelationStorage
from sqltide._native import ReplayOffsetProgress as ReplayOffsetProgress


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
