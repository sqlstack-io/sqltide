"""Shared detection of pending in-place row-error policy changes."""

import re

from sqltide.adapter.constants import QUARANTINE_LIVE_VIEW_PATTERN
from sqltide.adapter.models import CatalogRelation
from sqltide.compiler.compile.models import DesiredMaterializedView


def change_pending(
    *,
    target_name: str,
    view_name: str,
    dead_letter_view_name: str,
    relation_by_name: dict[str, CatalogRelation],
    dead_letter_name_owned_by_model: bool,
    quarantines: bool,
) -> bool:
    """Return whether the live views differ from the policy, repairing a half-applied switch."""

    if target_name not in relation_by_name:
        return False
    live_view: CatalogRelation | None = relation_by_name.get(view_name)
    has_dead_letter_view: bool = (
        dead_letter_view_name in relation_by_name and not dead_letter_name_owned_by_model
    )
    live_quarantines: bool = (
        live_view is not None
        and re.search(QUARANTINE_LIVE_VIEW_PATTERN, live_view.query_sql or "") is not None
    )
    if quarantines:
        return live_view is None or not live_quarantines or not has_dead_letter_view
    return has_dead_letter_view or live_quarantines


def landing_view_names(
    *, view: DesiredMaterializedView, views: tuple[DesiredMaterializedView, ...]
) -> tuple[str, ...]:
    """Return the landing view feeding this view, or nothing when it reads a landing table."""

    current: DesiredMaterializedView = view
    visited: set[str] = {current.name}
    while True:
        parent: DesiredMaterializedView | None = next(
            (
                candidate
                for candidate in views
                if candidate.target_table_name == current.source_table_name
                and not candidate.dead_letters
                and candidate.name not in visited
            ),
            None,
        )
        if parent is None:
            return () if current is view else (current.name,)
        visited.add(parent.name)
        current = parent
