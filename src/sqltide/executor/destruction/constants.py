"""Constants for destructive execution."""

from datetime import timedelta

DEFAULT_DESTRUCTION_PLAN_TTL: timedelta = timedelta(minutes=15)
DESTRUCTION_PLAN_PAYLOAD_VERSION: int = 1
DUPLICATE_RELATION_ORDERING_ERROR: str = "duplicate"
PRODUCTION_TARGET_NAMES: frozenset[str] = frozenset({"prod", "production"})
RESIDUAL_CATALOG_STATUS_OBSERVED: str = "observed"
