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
PIPELINE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "mode",
        "replay_on_change",
        "bounded_replay_fallback",
        "naming",
        "protection",
        "audit_defaults",
        "execution",
        "defaults",
    }
)
EXECUTION_SETTING_NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXECUTION_SETTINGS_KEYS: frozenset[str] = frozenset({"replay"})
PIPELINE_EXECUTION_KEYS: frozenset[str] = frozenset({"replay"})
PIPELINE_REPLAY_EXECUTION_KEYS: frozenset[str] = frozenset({"settings"})
NAMING_KEYS: frozenset[str] = frozenset({"table_prefix", "view_prefix"})
PROTECTION_KEYS: frozenset[str] = frozenset({"warning", "confirmation"})
PROTECTION_CONFIRMATION_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
PROTECTION_CONFIRMATION_UNSAFE_PATTERN: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._:-]")
DEFAULT_ADAPTER_NAME: str = "clickhouse"
PYTHON_PACKAGE_INITIALIZER_FILE_NAME: str = "__init__.py"
FULL_REPLAY_POLICY_VALUE: str = "full"
INTERPOLATION_TOKEN_START: str = "${"
MODEL_DEFAULT_KEYS: frozenset[str] = frozenset({"retention"})
PIPELINE_DEFAULT_KEYS: frozenset[str] = frozenset({"models"})
KAFKA_NAMING_MACRO_TOPIC_PARAMETER: str = "topic"
SOURCE_FILE_KEYS: frozenset[str] = frozenset({"sources"})
SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "kind",
        "broker_list",
        "topic",
        "consumer_group",
        "format",
        "ttl",
        "retention",
        "settings",
        "table_name",
        "replay_boundary",
        "freshness",
        "host",
        "port",
        "database",
        "table",
        "user",
        "password_env",
        "refresh",
        "append",
    }
)
POSTGRES_SOURCE_REQUIRED_KEYS: tuple[str, ...] = ("host", "database", "table", "user")
REFRESH_INTERVAL_PATTERN: re.Pattern[str] = re.compile(
    r"^\d+\s+(SECOND|MINUTE|HOUR|DAY)S?$", re.IGNORECASE
)
DEFAULT_POSTGRES_PORT: int = 5432
BOOLEAN_TRUE_LITERALS: frozenset[str] = frozenset({"true", "yes", "1"})
BOOLEAN_FALSE_LITERALS: frozenset[str] = frozenset({"false", "no", "0"})
POSTGRES_FORBIDDEN_SOURCE_KEYS: tuple[str, ...] = (
    "broker_list",
    "topic",
    "consumer_group",
    "format",
    "ttl",
    "retention",
    "settings",
    "table_name",
    "replay_boundary",
)
REPLAY_BOUNDARY_KEYS: frozenset[str] = frozenset({"mode", "columns"})
REPLAY_BOUNDARY_COLUMN_KEYS: frozenset[str] = frozenset(
    {
        "_replay_partition",
        "_replay_offset",
        "_replay_timestamp",
        "_replay_landed_at",
        "_replay_cursor",
    }
)
MODEL_COLUMN_KEYS: frozenset[str] = frozenset({"description", "audits"})
AUDIT_DEFAULT_KEYS: frozenset[str] = frozenset({"severity", "every", "warmup"})
AUDIT_SEVERITIES: frozenset[str] = frozenset({"error", "warning"})
AUDIT_DEFAULT_EVERY_KEY: str = "every"
AUDIT_DEFAULT_WARMUP_KEY: str = "warmup"
SECONDS_BY_DURATION_UNIT: dict[str, int] = {
    "d": 24 * 60 * 60,
    "h": 60 * 60,
    "m": 60,
    "s": 1,
}
