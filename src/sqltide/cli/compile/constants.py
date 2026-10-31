"""Compile artifact constants."""

RESERVED_TARGET_OWNER_NAMES: frozenset[str] = frozenset(
    {"compiled", "run", "manifest.json", "sqltide_dag.json"}
)
ARTIFACTS_MANIFEST_FIELD: str = "artifacts"
BROKER_USERINFO_SEPARATOR: str = "@"
SENSITIVE_SOURCE_SETTING_FRAGMENTS: tuple[str, ...] = (
    "credential",
    "password",
    "private_key",
    "sasl",
    "secret",
    "token",
)
