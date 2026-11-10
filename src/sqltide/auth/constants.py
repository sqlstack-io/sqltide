"""Stable authentication and account-control constants."""

from uuid import UUID

ADMIN_ROLE: str = "admin"
VIEWER_ROLE: str = "viewer"
ACCOUNT_SCHEMA_COMPONENT: str = "accounts"
PASSWORD_MIN_LENGTH: int = 12
SQLITE_MEMORY_PATH: str = ":memory:"
CONTROL_STORE_TABLE_PREFIX: str = "sqltide_"
STREAMBUILD_CONTROL_STORE_TABLE_PREFIX: str = "streambuild_"
LOCAL_USER_ID: UUID = UUID("a25809d6-cf14-592c-b7b6-c94ef048e0cc")
