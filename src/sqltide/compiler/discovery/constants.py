"""Discovery constants."""

from __future__ import annotations

import re

SCHEMA_CHANGE_RULE_KEYS: frozenset[str] = frozenset({"breaking", "non_breaking"})

MODEL_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*MODEL\s*\((?P<header>.*?)\)\s*;\s*(?P<sql>.*)$",
    re.DOTALL,
)
ALLOWED_MODEL_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "description",
        "columns",
        "audits",
        "relation_name",
        "engine",
        "order_by",
        "partition_by",
        "ttl",
        "retention",
        "settings",
        "execution_settings",
        "replay_anchor",
        "replay_on_change",
        "bounded_replay_fallback",
    }
)
VIEW_FORBIDDEN_MODEL_KEYS: frozenset[str] = frozenset(
    {
        "engine",
        "order_by",
        "partition_by",
        "ttl",
        "retention",
        "settings",
        "execution_settings",
        "replay_anchor",
        "replay_on_change",
        "bounded_replay_fallback",
    }
)
DEFAULT_SQL_MODEL_ENGINE: str = "MergeTree()"
DEFAULT_SQL_MODEL_ORDER_BY: tuple[str, ...] = ("_replay_timestamp",)
DEFAULT_PIPELINE_PREFIX: str = "pl__"
DEFAULT_TABLE_PREFIX: str = "tbl__"
DEFAULT_VIEW_PREFIX: str = "view__"
RUN_UNRESPONSIVE_AFTER_SECONDS: int = 45
DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS: int = 10 * 60
PROJECT_CONFIG_FILE_NAME: str = "sqltide_project.toml"
LOCAL_CONFIG_FILE_NAME: str = "sqltide_local.toml"
LEGACY_PROJECT_CONFIG_FILE_NAME: str = "streambuild_project.toml"
LEGACY_LOCAL_CONFIG_FILE_NAME: str = "streambuild_local.toml"

PIPELINE_CONFIG_FILE_NAME: str = "pipeline.toml"
EXECUTION_SETTING_NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXECUTION_SETTINGS_KEYS: frozenset[str] = frozenset({"replay"})
DEFAULT_ADAPTER_NAME: str = "clickhouse"
PYTHON_PACKAGE_INITIALIZER_FILE_NAME: str = "__init__.py"
KAFKA_NAMING_MACRO_TOPIC_PARAMETER: str = "topic"
DEFAULT_POSTGRES_PORT: int = 5432
MODEL_COLUMN_KEYS: frozenset[str] = frozenset({"description", "audits"})
SECONDS_BY_DURATION_UNIT: dict[str, int] = {
    "d": 24 * 60 * 60,
    "h": 60 * 60,
    "m": 60,
    "s": 1,
}
