"""Constants for destructive execution."""

from datetime import timedelta

CATALOG_MATERIALIZED_VIEW_ENGINE: str = "MaterializedView"
CATALOG_VIEW_ENGINE: str = "View"
DEFAULT_DESTRUCTION_PLAN_TTL: timedelta = timedelta(minutes=15)
DESTRUCTION_PLAN_PAYLOAD_VERSION: int = 1
DUPLICATE_RELATION_ORDERING_ERROR: str = "duplicate"
MAX_NAMED_CHALLENGES: int = 3
METADATA_RELATION_PREFIX: str = "_sqltide_"
PRODUCTION_TARGET_NAMES: frozenset[str] = frozenset({"prod", "production"})
RESIDUAL_CATALOG_STATUS_OBSERVED: str = "observed"
