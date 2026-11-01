"""Population exceptions."""

from sqltide.compiler.compile.models import ObjectKey
from sqltide.executor.population.constants import (
    MISSING_KEY_SCOPE_ERROR,
    MISSING_NAME_SCOPE_ERROR,
)


class PopulationScopeError(LookupError):
    """Raised when a population plan names an object its desired state lacks."""

    @classmethod
    def from_native(
        cls, *, error: ValueError, keys: tuple[ObjectKey, ...]
    ) -> "PopulationScopeError":
        """Translate a native population scope decision's lookup failure."""

        if error.args[0] == MISSING_KEY_SCOPE_ERROR:
            return cls(f"Population object '{keys[error.args[1]].name}' has no desired object")
        if error.args[0] == MISSING_NAME_SCOPE_ERROR:
            return cls(f"Population relation '{error.args[1]}' has no desired object")
        return cls("Population root has no owning replay root, table, or replay input")
