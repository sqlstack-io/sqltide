"""Order the view drops and creations of in-place row-error policy changes."""

from collections.abc import Callable

from sqltide.compiler.discovery.types import RowErrorPolicy
from sqltide.compiler.planner.models import DirectRowErrorPolicyChange


def row_error_policy_view_steps(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    database: str,
    render_view: Callable[[str], str | None],
) -> tuple[list[tuple[str, bool]], list[tuple[str, str]]]:
    """Return teardown and realization steps, never dropping a view that cannot be replaced."""

    teardown: list[tuple[str, bool]] = []
    realizations: list[tuple[str, str]] = []
    for change in (change for change in changes if not change.landing_repair_only):
        view_sql: str | None = render_view(change.logical_view_name)
        dead_letter_sql: str | None = render_view(change.logical_dead_letter_view_name)
        if view_sql is None or (
            dead_letter_sql is None and change.desired_policy is RowErrorPolicy.QUARANTINE
        ):
            continue
        if dead_letter_sql is not None:
            teardown.append((change.dead_letter_view_name, False))
            realizations.append((change.dead_letter_view_name, dead_letter_sql))
        realizations.append(
            (
                f"replace_{change.view_name}",
                f"DROP TABLE IF EXISTS {database}.{change.view_name} SYNC",
            )
        )
        realizations.append((change.view_name, view_sql))
        if dead_letter_sql is None:
            realizations.append(
                (
                    f"retire_{change.dead_letter_view_name}",
                    f"DROP TABLE IF EXISTS {database}.{change.dead_letter_view_name} SYNC",
                )
            )
    return teardown, realizations
