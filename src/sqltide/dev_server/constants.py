"""Dev server constants."""

DEFAULT_DEV_SERVER_HOST: str = "127.0.0.1"
DEFAULT_DEV_SERVER_PORT: int = 8000
REDACTED_SECRET_PLACEHOLDER: str = "<redacted>"
BROKER_USERINFO_SEPARATOR: str = "@"
KAFKA_REUSE_LOG_FIRST_READ_COUNT: int = 2
KAFKA_REUSE_LOG_INTERVAL: int = 100
SENSITIVE_SOURCE_SETTING_FRAGMENTS: tuple[str, ...] = (
    "credential",
    "password",
    "private_key",
    "sasl",
    "secret",
    "token",
)
CHECK_KIND_TEST: str = "test"
CHECK_KIND_AUDIT: str = "audit"
CANCEL_GRACE_SECONDS: float = 15.0
TERMINATE_GRACE_SECONDS: float = 3.0
BUILD_CANCELLATION_FAILED_STATUS: str = "cancellation_failed"
ACTIVITY_WINDOW_SECONDS: int = 120
THROUGHPUT_WINDOW_LADDER: tuple[tuple[int, int], ...] = (
    (3600, 60),
    (86400, 1800),
    (604800, 10800),
)
