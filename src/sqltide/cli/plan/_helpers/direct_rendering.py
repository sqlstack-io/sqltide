"""Render direct-mode plans as operator text and machine-readable JSON."""

from __future__ import annotations

import json

from sqltide.cli.plan.constants import DIRECT_MODE_LABEL
from sqltide.cli.presentation.main._cli_style import cli_style
from sqltide.compiler.compile.models import LogicalResourceKey, ObjectKey
from sqltide.compiler.planner.models import (
    DirectPlan,
    DirectPlanEntry,
    DirectPrerequisite,
    DirectRelationOperation,
    DirectReplayRoot,
    DirectRowErrorPolicyChange,
    DirectSourceDriftSelection,
    DirectSqlChange,
    PlannerWarning,
)
from sqltide.compiler.planner.types import DirectSqlBaselineStatus
from sqltide.executor.population.models import PopulationSourceRecreation

_DEFAULT_DIFF_LINE_LIMIT: int = 12


def render_direct_plan_json(
    *,
    plan: DirectPlan,
    adapter_name: str,
    source_recreations: tuple[PopulationSourceRecreation, ...] = (),
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = (),
) -> str:
    """Render one direct plan as deterministic JSON."""

    payload: dict[str, object] = {
        "mode": DIRECT_MODE_LABEL,
        "adapter": adapter_name,
        "database": plan.database,
        "start_time": plan.effective_start_time,
        "selection_mode": str(plan.selection_mode),
        "user_scope": [_logical_key_payload(key) for key in plan.user_scope],
        "execution_scope": [_logical_key_payload(key) for key in plan.execution_scope],
        "prerequisite_execution_scope": [
            _logical_key_payload(key) for key in plan.prerequisite_execution_scope
        ],
        "prerequisite_scope": [
            _prerequisite_payload(prerequisite) for prerequisite in plan.prerequisite_scope
        ],
        "entries": [_entry_payload(entry) for entry in plan.entries],
        "replay_roots": [_replay_root_payload(root) for root in plan.replay_roots],
        "teardown": [_operation_payload(operation) for operation in plan.teardown_operations],
        "creation": [_operation_payload(operation) for operation in plan.creation_operations],
        "source_drift_selections": [
            _source_drift_selection_payload(selection) for selection in plan.source_drift_selections
        ],
        "source_recreations": [
            _source_recreation_payload(recreation) for recreation in source_recreations
        ],
        "warnings": [_warning_payload(warning) for warning in plan.warnings],
    }
    if row_error_policy_changes:
        payload["row_error_policy_changes"] = [
            {
                "model_key": _logical_key_payload(change.model_key),
                "desired_policy": str(change.desired_policy),
                "view": change.view_name,
                "dead_letter_view": change.dead_letter_view_name,
                "operation": "view_only",
                "replay_roots": 0,
            }
            for change in row_error_policy_changes
        ]
    return json.dumps(payload, indent=2) + "\n"


def render_direct_plan_text(
    *,
    plan: DirectPlan,
    adapter_name: str,
    verbose: bool = False,
    source_recreations: tuple[PopulationSourceRecreation, ...] = (),
    row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...] = (),
) -> str:
    """Render one direct plan as operator-facing text."""

    lines: list[str] = [cli_style().title(f"Direct plan  {plan.database}"), ""]
    if verbose:
        lines.append(cli_style().label_value(label="Adapter", value=adapter_name))
        lines.append("")
    lines.extend(_render_rebuild_paths(plan=plan))
    lines.extend((cli_style().section("Model decisions"), ""))
    entry: DirectPlanEntry
    for entry in plan.entries:
        lines.extend(_render_model_decision(entry=entry, verbose=verbose))
    selection: DirectSourceDriftSelection
    for selection in plan.source_drift_selections:
        lines.append(f"{cli_style().label('source')}  {render_source_drift_selection(selection)}")
    recreation: PopulationSourceRecreation
    for recreation in source_recreations:
        lines.append(
            f"{cli_style().label('source')}  {render_source_recreation_notice(recreation)}"
        )
    if plan.effective_start_time is not None:
        lines.append(cli_style().label_value(label="Start time", value=plan.effective_start_time))
    warning: PlannerWarning
    for warning in plan.warnings:
        lines.append(cli_style().warning(f"warning  {warning.message}"))
    change: DirectRowErrorPolicyChange
    for change in row_error_policy_changes:
        lines.append(
            f"view-only  {change.model_key.name}: on_row_error -> {change.desired_policy}, "
            f"recreate {change.view_name}, no replay"
        )
    lines.append(_summary_line(plan=plan, row_error_policy_changes=row_error_policy_changes))
    return "\n".join(lines)


def _render_rebuild_paths(*, plan: DirectPlan) -> tuple[str, ...]:
    lines: list[str] = [cli_style().section("Rebuild paths"), ""]
    covered_model_keys: set[LogicalResourceKey] = set()
    roots_by_source: dict[tuple[str, str], list[DirectReplayRoot]] = {}
    replay_root: DirectReplayRoot
    for replay_root in plan.replay_roots:
        source_identity: tuple[str, str] = (
            replay_root.driving_input_relation_name,
            str(replay_root.replay_boundary_mode),
        )
        roots_by_source.setdefault(source_identity, []).append(replay_root)
    source_identity: tuple[str, str]
    replay_roots: list[DirectReplayRoot]
    for source_index, (source_identity, replay_roots) in enumerate(roots_by_source.items()):
        if source_index:
            lines.append("")
        lines.append(f"[replay source] {source_identity[0]} [{source_identity[1]}]")
        for root_index, replay_root in enumerate(replay_roots):
            root_is_last: bool = root_index == len(replay_roots) - 1
            root_branch: str = "└──" if root_is_last else "├──"
            lines.append(f"{root_branch} [replay root] {_key_name(replay_root.model_key)}")
            propagated_keys: tuple[LogicalResourceKey, ...] = tuple(
                key for key in replay_root.propagated_model_keys if key != replay_root.model_key
            )
            child_prefix: str = "    " if root_is_last else "│   "
            for child_index, model_key in enumerate(propagated_keys):
                child_branch: str = "└──" if child_index == len(propagated_keys) - 1 else "├──"
                lines.append(f"{child_prefix}{child_branch} [rebuild] {_key_name(model_key)}")
            covered_model_keys.update(replay_root.propagated_model_keys)
    entry: DirectPlanEntry
    for entry in plan.entries:
        if entry.model_key not in covered_model_keys:
            lines.append(f"[rebuild] {_key_name(entry.model_key)}")
    lines.append("")
    return tuple(lines)


def _render_model_decision(*, entry: DirectPlanEntry, verbose: bool) -> tuple[str, ...]:
    lines: list[str] = [cli_style().object_name(text=_key_name(entry.model_key))]
    if entry.sql_change is not None:
        lines.extend(_render_sql_change(sql_change=entry.sql_change, verbose=verbose))
    lines.append(f"  {cli_style().label('reason')}  {entry.reason}")
    lines.append(f"  {cli_style().label('rebuild')}  {', '.join(entry.relation_names)}")
    lines.append("")
    return tuple(lines)


def _render_sql_change(*, sql_change: DirectSqlChange, verbose: bool) -> tuple[str, ...]:
    status: DirectSqlBaselineStatus = DirectSqlBaselineStatus(sql_change.status)
    label: str = {
        DirectSqlBaselineStatus.FIRST_BASELINE: "first SQL baseline",
        DirectSqlBaselineStatus.QUERY_CHANGED: "SQL changed",
        DirectSqlBaselineStatus.NO_QUERY_CHANGE: "no SQL change",
        DirectSqlBaselineStatus.BASELINE_UNAVAILABLE: "SQL baseline unavailable",
    }[status]
    lines: list[str] = [f"  {label}"]
    if sql_change.warning is not None:
        lines.append(f"    {cli_style().warning(sql_change.warning)}")
    if sql_change.unified_diff is not None:
        diff_lines: tuple[str, ...] = tuple(sql_change.unified_diff.splitlines())
        visible_lines: tuple[str, ...] = (
            diff_lines if verbose else diff_lines[:_DEFAULT_DIFF_LINE_LIMIT]
        )
        lines.extend(f"    {line}" for line in cli_style().diff_lines(visible_lines))
        if not verbose and len(diff_lines) > len(visible_lines):
            lines.append(cli_style().muted("    ... use --verbose for the complete diff"))
    return tuple(lines)


def render_source_recreation_notice(recreation: PopulationSourceRecreation) -> str:
    """Describe one drifted managed source recreation in a single operator-facing line."""

    return (
        f"{recreation.managed_source.resource.name} and {recreation.landing_view_name} are "
        "recreated with the declared settings "
        f"({', '.join(recreation.drifted_setting_names)}); {recreation.landing_relation_name} "
        "and its rows are kept. Consumption resumes from the committed offsets of consumer "
        f"group '{recreation.consumer_group}', or follows the Kafka engine's offset reset "
        "policy on a topic where the group has none."
    )


def render_source_drift_selection(selection: DirectSourceDriftSelection) -> str:
    """Explain which owning pipeline a changed selection rebuilds for one drifted source."""

    other_readers: tuple[str, ...] = tuple(
        name for name in selection.reader_pipeline_names if name != selection.pipeline_name
    )
    kept_note: str = (
        f"; {', '.join(other_readers)} keep reading the preserved landing table"
        if other_readers
        else ""
    )
    return (
        f"{selection.source_key.name} settings drifted "
        f"({', '.join(selection.drifted_setting_names)}); --changed rebuilds "
        f"pipeline:{selection.pipeline_name} to recreate it{kept_note}"
    )


def _source_drift_selection_payload(selection: DirectSourceDriftSelection) -> dict[str, object]:
    return {
        "source_name": selection.source_key.name,
        "pipeline_name": selection.pipeline_name,
        "reader_pipeline_names": list(selection.reader_pipeline_names),
        "drifted_settings": list(selection.drifted_setting_names),
        "model_keys": [_logical_key_payload(key) for key in selection.model_keys],
        "message": render_source_drift_selection(selection),
    }


def _source_recreation_payload(recreation: PopulationSourceRecreation) -> dict[str, object]:
    return {
        "relation_name": recreation.managed_source.resource.name,
        "landing_view_name": recreation.landing_view_name,
        "landing_relation_name": recreation.landing_relation_name,
        "consumer_group": recreation.consumer_group,
        "drifted_settings": list(recreation.drifted_setting_names),
        "message": render_source_recreation_notice(recreation),
    }


def _summary_line(
    *, plan: DirectPlan, row_error_policy_changes: tuple[DirectRowErrorPolicyChange, ...]
) -> str:
    model_count: int = len(plan.execution_scope)
    relation_count: int = len(plan.creation_operations)
    replay_count: int = len(plan.replay_roots)
    policy_count: int = len(row_error_policy_changes)
    policy_suffix: str = (
        ""
        if policy_count == 0
        else f", {policy_count} view-only row-error policy change"
        f"{'s' if policy_count != 1 else ''} (no replay)"
    )
    return (
        f"{model_count} model{'s' if model_count != 1 else ''} selected, "
        f"{relation_count} relation{'s' if relation_count != 1 else ''} rebuilt, "
        f"{replay_count} replay root{'s' if replay_count != 1 else ''}{policy_suffix}"
    )


def _prerequisite_payload(prerequisite: DirectPrerequisite) -> dict[str, object]:
    return {
        "key": _logical_key_payload(prerequisite.key),
        "relation_names": list(prerequisite.relation_names),
        "present": prerequisite.present,
        "framework_managed": prerequisite.framework_managed,
    }


def _entry_payload(entry: DirectPlanEntry) -> dict[str, object]:
    return {
        "model_key": _logical_key_payload(entry.model_key),
        "reason": str(entry.reason),
        "relation_names": list(entry.relation_names),
        "resource_kinds": [str(kind) for kind in entry.resource_kinds],
        "driving_input_key": (
            None
            if entry.driving_input_key is None
            else _logical_key_payload(entry.driving_input_key)
        ),
        "is_replay_root": entry.is_replay_root,
        "sql_change": (None if entry.sql_change is None else _sql_change_payload(entry.sql_change)),
    }


def _sql_change_payload(sql_change: DirectSqlChange) -> dict[str, object]:
    return {
        "status": str(sql_change.status),
        "current_sql": sql_change.current_sql,
        "current_hash": sql_change.current_hash,
        "previous_sql": sql_change.previous_sql,
        "previous_hash": sql_change.previous_hash,
        "unified_diff": sql_change.unified_diff,
        "warning": sql_change.warning,
    }


def _replay_root_payload(root: DirectReplayRoot) -> dict[str, object]:
    return {
        "model_key": _logical_key_payload(root.model_key),
        "driving_input_key": _logical_key_payload(root.driving_input_key),
        "driving_input_relation_name": root.driving_input_relation_name,
        "driving_input_replay_columns": {
            "partition": root.driving_input_replay_columns.partition,
            "offset": root.driving_input_replay_columns.offset,
            "timestamp": root.driving_input_replay_columns.timestamp,
            "landed_at": root.driving_input_replay_columns.landed_at,
            "cursor": root.driving_input_replay_columns.cursor,
        },
        "replay_boundary_mode": str(root.replay_boundary_mode),
        "propagated_model_keys": [_logical_key_payload(key) for key in root.propagated_model_keys],
        "has_aggregate_semantics": root.has_aggregate_semantics,
        "settings": dict(root.replay_settings),
    }


def _operation_payload(operation: DirectRelationOperation) -> dict[str, object]:
    return {
        "relation_name": operation.relation_name,
        "action": str(operation.action),
        "model_key": _logical_key_payload(operation.model_key),
        "resource_kind": str(operation.resource_kind),
    }


def _warning_payload(warning: PlannerWarning) -> dict[str, object]:
    return {
        "warning_code": warning.warning_code,
        "message": warning.message,
        "root_key": _object_key_payload(warning.root_key),
        "target_key": (
            None if warning.target_key is None else _object_key_payload(warning.target_key)
        ),
    }


def _key_name(key: LogicalResourceKey) -> str:
    return key.name


def _logical_key_payload(key: LogicalResourceKey) -> dict[str, str]:
    return {"resource_type": str(key.resource_type), "name": key.name}


def _object_key_payload(key: ObjectKey) -> dict[str, str | None]:
    return {"database": key.database, "object_type": key.object_type, "name": key.name}
