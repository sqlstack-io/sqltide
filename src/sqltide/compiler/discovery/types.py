"""Authored spec domain types."""

from enum import StrEnum


class PipelineMode(StrEnum):
    DIRECT = "direct"
    VIRTUAL = "virtual"


class RetentionMissingBehavior(StrEnum):
    ERROR = "error"
    SKIP = "skip"


class KafkaRetentionReference(StrEnum):
    BROKER = "broker"
    LANDED = "landed"


class KafkaRetentionOrigin(StrEnum):
    SOURCE = "source"
    PROJECT = "project"


class ModelReferenceScope(StrEnum):
    PROJECT = "project"
    PIPELINE = "pipeline"


class ReplayAnchorMode(StrEnum):
    AUTO = "auto"
    NEVER = "never"


class ReplayLineageMode(StrEnum):
    OFFSETS = "offsets"
    TIMESTAMP = "timestamp"
    LANDED_AT = "landed_at"
    CURSOR = "cursor"


class SourceKind(StrEnum):
    KAFKA = "kafka"
    STREAM_TABLE = "stream_table"
    POSTGRES = "postgres"


class SourceNameOrigin(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"


class ModelKind(StrEnum):
    TABLE = "table"
    VIEW = "view"


class ReplayBoundaryMode(StrEnum):
    OFFSETS = "offsets"
    TIMESTAMP = "timestamp"
    LANDED_AT = "landed_at"
    CURSOR = "cursor"


class BoundedReplayFallback(StrEnum):
    FULL = "full"
    BOUNDED_WITHOUT_HISTORY = "bounded_without_history"


class RefType(StrEnum):
    REFERENCE = "reference"
    MUTABLE = "mutable"


class SqlRelationType(StrEnum):
    SOURCE = "source"
    REF = "ref"


class ReplayOnChangeMode(StrEnum):
    FULL = "full"
    BOUNDED = "bounded"


class SchemaChangeKind(StrEnum):
    BREAKING = "breaking"
    NON_BREAKING = "non_breaking"


type ModelFacts = tuple[str, bool, bool, bool]
type PipelineFacts = tuple[str, str, bool, bool, bool, list[ModelFacts]]
type ProjectPolicyFacts = tuple[str, bool, bool, bool]
