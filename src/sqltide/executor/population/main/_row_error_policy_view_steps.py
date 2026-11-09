"""Order the view drops and creations of in-place row-error policy changes."""

from collections.abc import Callable

from sqltide.compiler.planner.models import DirectRowErrorPolicyChange


def row_error_policy_view_steps(
    *,
    changes: tuple[DirectRowErrorPolicyChange, ...],
    database: str,
    render_view: Callable[[str], str | None],
) -> tuple[list[tuple[str, bool]], list[tuple[str, str]]]:
    """Return teardown and realization steps, creating a dead-letter view before its model view."""

    teardown: list[tuple[str, bool]] = []
    realizations: list[tuple[str, str]] = []
    for change in changes:
        dead_letter_sql: str | None = render_view(change.logical_dead_letter_view_name)
        if dead_letter_sql is not None:
            teardown.append((change.dead_letter_view_name, False))
            realizations.append((change.dead_letter_view_name, dead_letter_sql))
        realizations.append(
            (
                f"replace_{change.view_name}",
                f"DROP TABLE IF EXISTS {database}.{change.view_name} SYNC",
            )
        )
        view_sql: str | None = render_view(change.logical_view_name)
        if view_sql is not None:
            realizations.append((change.view_name, view_sql))
        if dead_letter_sql is None:
            realizations.append(
                (
                    f"retire_{change.dead_letter_view_name}",
                    f"DROP TABLE IF EXISTS {database}.{change.dead_letter_view_name} SYNC",
                )
            )
    return teardown, realizations
