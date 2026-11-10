"""Public effective-capability summary entry point."""

from sqltide.auth.classes.control_store import ControlStore
from sqltide.authorization.classes.project_authorizer import ProjectAuthorizer
from sqltide.authorization.models import CapabilityRequest, EffectiveCapabilities


def effective_capabilities(
    *, store: ControlStore, request: CapabilityRequest
) -> EffectiveCapabilities:
    """Summarize one user's effective operational permissions."""

    return ProjectAuthorizer(store=store).capabilities(request=request)
