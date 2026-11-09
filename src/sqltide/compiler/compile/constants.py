"""Compile-phase constants."""

from sqltide.compiler.compile.models import Column
from sqltide.compiler.compile.types import DesiredObjectType

DESIRED_OBJECT_TYPE_KAFKA_TABLE: DesiredObjectType = DesiredObjectType(
    DesiredObjectType.KAFKA_TABLE
)
DESIRED_OBJECT_TYPE_TABLE: DesiredObjectType = DesiredObjectType(DesiredObjectType.TABLE)
DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW: DesiredObjectType = DesiredObjectType(
    DesiredObjectType.MATERIALIZED_VIEW
)
DESIRED_OBJECT_TYPE_VIEW: DesiredObjectType = DesiredObjectType(DesiredObjectType.VIEW)

KAFKA_TABLE_NAME_PREFIX: str = "kafka__"
RAW_TABLE_NAME_PREFIX: str = "raw__"
TRANSFORM_TABLE_NAME_PREFIX: str = "tbl__"
MATERIALIZED_VIEW_NAME_PREFIX: str = "mv__"

MANAGED_OBJECT_NAME_PREFIXES: tuple[str, ...] = (
    KAFKA_TABLE_NAME_PREFIX,
    RAW_TABLE_NAME_PREFIX,
    TRANSFORM_TABLE_NAME_PREFIX,
    MATERIALIZED_VIEW_NAME_PREFIX,
)

KAFKA_PARTITION_COLUMN_NAME: str = "kafka_partition"
KAFKA_OFFSET_COLUMN_NAME: str = "kafka_offset"
KAFKA_TIMESTAMP_COLUMN_NAME: str = "kafka_timestamp"
KAFKA_LANDED_AT_COLUMN_NAME: str = "kafka_landed_at"

REPLAY_PARTITION_COLUMN_NAME: str = "_replay_partition"
REPLAY_OFFSET_COLUMN_NAME: str = "_replay_offset"
REPLAY_TIMESTAMP_COLUMN_NAME: str = "_replay_timestamp"
REPLAY_LANDED_AT_COLUMN_NAME: str = "_replay_landed_at"
REPLAY_CURSOR_COLUMN_NAME: str = "_replay_cursor"

REPLAY_OFFSET_LINEAGE_COLUMN_NAMES: tuple[str, str] = (
    REPLAY_PARTITION_COLUMN_NAME,
    REPLAY_OFFSET_COLUMN_NAME,
)
REPLAY_TIME_LINEAGE_COLUMN_NAMES: tuple[str, str] = (
    REPLAY_TIMESTAMP_COLUMN_NAME,
    REPLAY_LANDED_AT_COLUMN_NAME,
)
REPLAY_ALL_COLUMN_NAMES: tuple[str, str, str, str, str] = (
    REPLAY_PARTITION_COLUMN_NAME,
    REPLAY_OFFSET_COLUMN_NAME,
    REPLAY_TIMESTAMP_COLUMN_NAME,
    REPLAY_LANDED_AT_COLUMN_NAME,
    REPLAY_CURSOR_COLUMN_NAME,
)
REPLAY_REQUIRED_COLUMN_NAMES_BY_MODE: dict[str, tuple[str, ...]] = {
    "offsets": REPLAY_OFFSET_LINEAGE_COLUMN_NAMES,
    "timestamp": (REPLAY_TIMESTAMP_COLUMN_NAME,),
    "landed_at": (REPLAY_LANDED_AT_COLUMN_NAME,),
    "cursor": (REPLAY_CURSOR_COLUMN_NAME,),
}


RAW_LANDING_COLUMNS: tuple[Column, ...] = (
    Column(name="kafka_key", type="String"),
    Column(name="kafka_value", type="String"),
    Column(name="kafka_topic", type="String"),
    Column(name=KAFKA_PARTITION_COLUMN_NAME, type="Int32"),
    Column(name=KAFKA_OFFSET_COLUMN_NAME, type="Int64"),
    Column(name=KAFKA_TIMESTAMP_COLUMN_NAME, type="Nullable(DateTime64(3))"),
    Column(name=REPLAY_PARTITION_COLUMN_NAME, type="Int32"),
    Column(name=REPLAY_OFFSET_COLUMN_NAME, type="Int64"),
    Column(name=REPLAY_TIMESTAMP_COLUMN_NAME, type="Nullable(DateTime64(3))"),
    Column(name="kafka_header_keys", type="Array(String)"),
    Column(name="kafka_header_values", type="Array(String)"),
    Column(name=KAFKA_LANDED_AT_COLUMN_NAME, type="DateTime64(3)"),
    Column(name=REPLAY_LANDED_AT_COLUMN_NAME, type="DateTime64(3)"),
)

AGGREGATING_ENGINE_NAMES: tuple[str, ...] = ("summingmergetree", "aggregatingmergetree")

SOURCE_REF_FUNCTION_NAME: str = "__source"
MODEL_REF_FUNCTION_NAME: str = "__ref"
REF_FUNCTION_NAMES: frozenset[str] = frozenset({SOURCE_REF_FUNCTION_NAME, MODEL_REF_FUNCTION_NAME})

SUPPORTED_KAFKA_TABLE_FORMAT: str = "JSONAsString"
FRAMEWORK_OWNED_KAFKA_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "kafka_broker_list",
        "kafka_topic_list",
        "kafka_group_name",
        "kafka_format",
    }
)

REF_TYPE_KEYWORD: str = "ref_type"
VALID_REF_ARGUMENT_COUNTS: frozenset[int] = frozenset({1, 2})
REF_WITH_TYPE_ARGUMENT_COUNT: int = 2
