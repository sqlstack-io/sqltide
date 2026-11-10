"""Population exceptions."""


class PopulationScopeError(LookupError):
    """Raised when a population plan names an object its desired state lacks."""
