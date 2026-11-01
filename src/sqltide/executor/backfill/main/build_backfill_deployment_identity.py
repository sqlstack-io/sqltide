"""Fix one virtual build identity before connected planning."""

from datetime import UTC, datetime
from uuid import uuid4

import sqltide._native as _native
from sqltide.executor.backfill._helpers.timing import build_current_timestamp
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.backfill.models import BackfillDeploymentIdentity


def build_backfill_deployment_identity(*, deployment_id: str | None) -> BackfillDeploymentIdentity:
    """Build the identity shared by a confirmed plan and its execution."""

    current_timestamp: str = build_current_timestamp()
    resolved_deployment_id: str = deployment_id or _generated_deployment_id(
        created_at=current_timestamp
    )
    if not _native.is_deployment_id(resolved_deployment_id):
        raise BackfillExecutionError(
            "Deployment ID must match YYYYMMDDTHHMMSSZ_<alphanumeric-suffix>"
        )
    created_at: str = (
        _created_at_from_deployment_id(deployment_id=resolved_deployment_id)
        if deployment_id is not None
        else current_timestamp
    )
    return BackfillDeploymentIdentity(
        deployment_id=resolved_deployment_id,
        created_at=created_at,
    )


def _generated_deployment_id(*, created_at: str) -> str:
    timestamp: datetime = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    return f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:6]}"


def _created_at_from_deployment_id(*, deployment_id: str) -> str:
    try:
        timestamp: datetime = datetime.strptime(deployment_id.partition("_")[0], "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise BackfillExecutionError(
            "Deployment ID timestamp must be a valid UTC date and time"
        ) from error
    return timestamp.strftime("%Y-%m-%d %H:%M:%S.000")
