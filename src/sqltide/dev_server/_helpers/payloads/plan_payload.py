"""Selector expansion, replay row counts, and DirectPlan serialization for /api/plan."""

from __future__ import annotations

from sqltide._native import render_source_recreation_notice
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterReplayColumns
from sqltide.cli.build.models import (
    BuildProtectionRequirement,
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
)
from sqltide.compiler.compile.models import CompiledPipeline, LogicalResourceKey, ObjectKey
from sqltide.compiler.discovery.models import PipelineProtection
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.models import (
    DeploymentStep,
    DirectPlan,
    DirectPlanEntry,
    DirectPrerequisite,
    DirectRelationOperation,
    DirectReplayRoot,
    DirectSourceDriftSelection,
    DirectSqlChange,
    PlannerWarning,
    PreparedShadowObject,
)
from sqltide.dev_server.exceptions import DevServerError
from sqltide.executor.population.models import PopulationSourceRecreation

_SOURCE_RECREATION_WARNING_CODE: str = "managed_source_recreated"
_TIMESTAMP_DRIVEN_MODES: frozenset[ReplayLineageMode] = frozenset(
    {ReplayLineageMode.TIMESTAMP, ReplayLineageMode.CURSOR}
)


def build_plan_payload(
    *,
    plan: DirectPlan,
    analysis: CompileAnalysis,
    selectors: tuple[str, ...],
    start_time: str | None,
    planned_at: str,
    replay_row_counts: dict[str, int | None],
    command: str,
) -> dict[str, object]:
    """Serialize one DirectPlan into the UI plan shape."""

    pipeline_by_model: dict[str, str] = {
        model.key.name: model.pipeline_name for model in analysis.compiled_project.models
    }
    return {
        "database": plan.database,
        "userScope": list(selectors),
        "entries": [
            _entry_payload(entry=entry, pipeline_by_model=pipeline_by_model)
            for entry in plan.entries
        ],
        "prerequisites": [_prerequisite_payload(item) for item in plan.prerequisite_scope],
        "teardown": [_operation_payload(item) for item in plan.teardown_operations],
        "creation": [_operation_payload(item) for item in plan.creation_operations],
        "replayRoots": [
            _replay_root_payload(
                item=item, rows_to_replay=replay_row_counts.get(item.model_key.name)
            )
            for item in plan.replay_roots
        ],
        "warnings": [
            {"code": item.warning_code, "message": item.message} for item in plan.warnings
        ],
        "protections": _protection_payloads(plan=plan, analysis=analysis),
        "replayWindow": (
            {"mode": "full"} if start_time is None else {"mode": "from", "startTime": start_time}
        ),
        "plannedAt": planned_at,
        "command": command,
    }


def build_mode_aware_plan_payload(
    *,
    preparation: DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation,
    analysis: CompileAnalysis,
    selectors: tuple[str, ...],
    planned_at: str,
    replay_row_counts: dict[str, int | None],
    command: str,
) -> dict[str, object]:
    """Serialize the exact mode-aware preparation the UI can execute."""

    direct: DirectWorkflowPreparation | None = (
        preparation.direct
        if isinstance(preparation, MixedWorkflowPreparation)
        else preparation
        if isinstance(preparation, DirectWorkflowPreparation)
        else None
    )
    virtual: VirtualWorkflowPreparation | None = (
        preparation.virtual
        if isinstance(preparation, MixedWorkflowPreparation)
        else preparation
        if isinstance(preparation, VirtualWorkflowPreparation)
        else None
    )
    if direct is not None:
        start_time: str | None = direct.preview.effective_start_time
        payload: dict[str, object] = build_plan_payload(
            plan=direct.preview.plan,
            analysis=analysis,
            selectors=selectors,
            start_time=start_time,
            planned_at=planned_at,
            replay_row_counts=replay_row_counts,
            command=command,
        )
    else:
        if virtual is None:
            raise DevServerError(
                "Cannot serialize a workflow preparation without a direct or virtual phase"
            )
        start_time = virtual.preview.start_time
        payload = _empty_plan_payload(
            database=virtual.preview.database,
            selectors=selectors,
            start_time=start_time,
            planned_at=planned_at,
            command=command,
        )
    source_name_by_relation: dict[str, str] = _source_name_by_relation(analysis=analysis)
    recreations: tuple[PopulationSourceRecreation, ...] = (
        () if direct is None else direct.workflow.recreated_sources
    )
    drift_selection_by_source: dict[str, DirectSourceDriftSelection] = (
        {}
        if direct is None
        else {
            selection.source_key.name: selection
            for selection in direct.preview.plan.source_drift_selections
        }
    )
    phases: list[dict[str, object]] = []
    if virtual is not None:
        phases.append(_virtual_phase_payload(preparation=virtual))
    if direct is not None:
        phases.append(
            _direct_phase_payload(
                preparation=direct,
                source_name_by_relation=source_name_by_relation,
            )
        )
    mode: str = (
        "mixed"
        if isinstance(preparation, MixedWorkflowPreparation)
        else "virtual"
        if isinstance(preparation, VirtualWorkflowPreparation)
        else "direct"
    )
    requirements: tuple[BuildProtectionRequirement, ...] = preparation.protection_requirements
    payload.update(
        {
            "mode": mode,
            "executionOrder": [str(phase["mode"]) for phase in phases],
            "phases": phases,
            "deploymentId": None if virtual is None else virtual.preview.deployment_id,
            "protections": [
                {
                    "pipelineName": requirement.pipeline_name,
                    "warning": requirement.warning,
                    "confirmation": requirement.confirmation,
                }
                for requirement in requirements
            ],
            "upperBoundary": {
                "mode": "captured_at_execution",
                "continuesLive": True,
            },
            "sourceRecreations": [
                _source_recreation_payload(
                    recreation=recreation,
                    source_name_by_relation=source_name_by_relation,
                    drift_selection_by_source=drift_selection_by_source,
                )
                for recreation in recreations
            ],
        }
    )
    if direct is not None and direct.workflow.recreated_sources:
        direct_warnings: object = payload["warnings"]
        payload["warnings"] = [
            *(direct_warnings if isinstance(direct_warnings, list) else []),
            *[
                {
                    "code": _SOURCE_RECREATION_WARNING_CODE,
                    "message": render_source_recreation_notice(recreation),
                    "relatedModel": None,
                }
                for recreation in direct.workflow.recreated_sources
            ],
        ]
    if virtual is not None:
        existing_warnings: object = payload["warnings"]
        model_name_by_object: dict[ObjectKey, str] = {
            prepared.logical_key: prepared.logical_model_name
            for prepared in virtual.preview.plan.prepared_shadow_objects
        }
        payload["warnings"] = [
            *(existing_warnings if isinstance(existing_warnings, list) else []),
            *[
                _virtual_warning_payload(
                    warning=warning,
                    model_name_by_object=model_name_by_object,
                )
                for warning in virtual.preview.plan.warnings
            ],
        ]
    return payload


def _empty_plan_payload(
    *,
    database: str,
    selectors: tuple[str, ...],
    start_time: str | None,
    planned_at: str,
    command: str,
) -> dict[str, object]:
    return {
        "database": database,
        "userScope": list(selectors),
        "entries": [],
        "prerequisites": [],
        "teardown": [],
        "creation": [],
        "replayRoots": [],
        "warnings": [],
        "sourceRecreations": [],
        "protections": [],
        "replayWindow": (
            {"mode": "full"} if start_time is None else {"mode": "from", "startTime": start_time}
        ),
        "plannedAt": planned_at,
        "command": command,
    }


def _source_name_by_relation(*, analysis: CompileAnalysis) -> dict[str, str]:
    names: dict[str, str] = {}
    key: LogicalResourceKey
    for key, resources in analysis.realized_project.resources_by_logical_key.items():
        names.update((resource.name, key.name) for resource in resources)
    return names


def _source_recreation_payload(
    *,
    recreation: PopulationSourceRecreation,
    source_name_by_relation: dict[str, str],
    drift_selection_by_source: dict[str, DirectSourceDriftSelection],
) -> dict[str, object]:
    relation_name: str = recreation.managed_source.resource.name
    source_name: str = source_name_by_relation.get(relation_name, relation_name)
    selection: DirectSourceDriftSelection | None = drift_selection_by_source.get(source_name)
    return {
        "sourceName": source_name,
        "relationName": relation_name,
        "landingViewName": recreation.landing_view_name,
        "landingRelationName": recreation.landing_relation_name,
        "stagedRelationName": recreation.staged_relation_name,
        "consumerGroup": recreation.consumer_group,
        "driftedSettings": list(recreation.drifted_setting_names),
        "message": render_source_recreation_notice(recreation),
        "selectedPipeline": None if selection is None else selection.pipeline_name,
        "otherReaderPipelines": (
            []
            if selection is None
            else [
                name for name in selection.reader_pipeline_names if name != selection.pipeline_name
            ]
        ),
    }


def _source_recreation_actions(
    *,
    recreations: tuple[PopulationSourceRecreation, ...],
    source_name_by_relation: dict[str, str],
) -> list[dict[str, object]]:
    """List the stage, swap, and retire steps that replace each drifted Kafka source."""

    actions: list[dict[str, object]] = []
    recreation: PopulationSourceRecreation
    for recreation in recreations:
        relation_name: str = recreation.managed_source.resource.name
        source_name: str = source_name_by_relation.get(relation_name, relation_name)
        action: str
        physical_name: str
        for action, physical_name in (
            ("stage", recreation.staged_relation_name),
            ("swap", relation_name),
            ("retire", recreation.staged_relation_name),
        ):
            actions.append(
                {
                    "phase": "preparation",
                    "action": action,
                    "logicalName": source_name,
                    "physicalName": physical_name,
                }
            )
    return actions


def _direct_phase_payload(
    *, preparation: DirectWorkflowPreparation, source_name_by_relation: dict[str, str]
) -> dict[str, object]:
    plan: DirectPlan = preparation.preview.plan
    relation_names: list[str] = []
    for entry in plan.entries:
        relation_names.extend(entry.relation_names)
    return {
        "mode": "direct",
        "effect": "applied_immediately",
        "deploymentId": None,
        "modelNames": [entry.model_key.name for entry in plan.entries],
        "contextModelNames": [item.key.name for item in plan.prerequisite_scope],
        "relationNames": relation_names,
        "actions": _source_recreation_actions(
            recreations=preparation.workflow.recreated_sources,
            source_name_by_relation=source_name_by_relation,
        )
        + [
            {
                "phase": "teardown",
                "action": str(operation.action),
                "logicalName": operation.model_key.name,
                "physicalName": operation.relation_name,
            }
            for operation in plan.teardown_operations
        ]
        + [
            {
                "phase": "creation",
                "action": str(operation.action),
                "logicalName": operation.model_key.name,
                "physicalName": operation.relation_name,
            }
            for operation in plan.creation_operations
        ],
        "startTime": preparation.preview.effective_start_time,
    }


def _virtual_phase_payload(*, preparation: VirtualWorkflowPreparation) -> dict[str, object]:
    prepared_objects: tuple[PreparedShadowObject, ...] = (
        preparation.preview.plan.prepared_shadow_objects
    )
    model_name_by_object: dict[ObjectKey, str] = {
        prepared.logical_key: prepared.logical_model_name for prepared in prepared_objects
    }
    model_names: list[str] = []
    for key in preparation.preview.run_execution_scope:
        if key.name not in model_names:
            model_names.append(key.name)
    return {
        "mode": "virtual",
        "effect": "staged",
        "deploymentId": preparation.preview.deployment_id,
        "modelNames": model_names,
        "contextModelNames": [key.name for key in preparation.preview.run_context_scope],
        "relationNames": [prepared.physical_name for prepared in prepared_objects],
        "actions": [
            _virtual_action_payload(step=step, model_name_by_object=model_name_by_object)
            for step in preparation.preview.plan.steps
        ],
        "startTime": preparation.preview.start_time,
    }


def _virtual_action_payload(
    *, step: DeploymentStep, model_name_by_object: dict[ObjectKey, str]
) -> dict[str, object]:
    affected_key: ObjectKey = step.target_key or step.root_key
    return {
        "phase": str(step.phase),
        "action": str(step.action),
        "logicalName": model_name_by_object.get(affected_key, affected_key.name),
        "physicalName": step.physical_name,
    }


def _virtual_warning_payload(
    *, warning: PlannerWarning, model_name_by_object: dict[ObjectKey, str]
) -> dict[str, object]:
    affected_key: ObjectKey = warning.target_key or warning.root_key
    return {
        "code": warning.warning_code,
        "message": warning.message,
        "relatedModel": model_name_by_object.get(affected_key, affected_key.name),
    }


def _protection_payloads(*, plan: DirectPlan, analysis: CompileAnalysis) -> list[dict[str, str]]:
    execution_keys: frozenset[LogicalResourceKey] = frozenset(plan.execution_scope)
    payloads: list[dict[str, str]] = []
    pipeline: CompiledPipeline
    for pipeline in analysis.compiled_project.pipelines:
        protection: PipelineProtection | None = pipeline.pipeline.protection
        if protection is None or not any(model.key in execution_keys for model in pipeline.models):
            continue
        payloads.append(
            {
                "pipelineName": pipeline.pipeline.name,
                "warning": protection.warning,
                "confirmation": protection.confirmation,
            }
        )
    return payloads


def _entry_payload(
    *,
    entry: DirectPlanEntry,
    pipeline_by_model: dict[str, str],
) -> dict[str, object]:
    return {
        "modelName": entry.model_key.name,
        "pipeline": pipeline_by_model.get(entry.model_key.name),
        "reason": str(entry.reason),
        "relationNames": list(entry.relation_names),
        "resourceKinds": [str(kind) for kind in entry.resource_kinds],
        "drivingInput": None if entry.driving_input_key is None else entry.driving_input_key.name,
        "isReplayRoot": entry.is_replay_root,
        "sqlChange": _sql_change_payload(entry.sql_change),
    }


def _sql_change_payload(sql_change: DirectSqlChange | None) -> dict[str, object] | None:
    if sql_change is None:
        return None
    return {
        "status": str(sql_change.status),
        "unifiedDiff": sql_change.unified_diff,
        "warning": sql_change.warning,
    }


def _prerequisite_payload(item: DirectPrerequisite) -> dict[str, object]:
    return {
        "name": item.key.name,
        "type": str(item.key.resource_type),
        "relationNames": list(item.relation_names),
        "present": item.present,
        "frameworkManaged": item.framework_managed,
    }


def _operation_payload(item: DirectRelationOperation) -> dict[str, object]:
    return {
        "relationName": item.relation_name,
        "action": str(item.action),
        "modelName": item.model_key.name,
        "resourceKind": str(item.resource_kind),
    }


def _replay_root_payload(
    *, item: DirectReplayRoot, rows_to_replay: int | None
) -> dict[str, object]:
    return {
        "modelName": item.model_key.name,
        "drivingInputName": item.driving_input_key.name,
        "drivingInputRelationName": item.driving_input_relation_name,
        "boundaryMode": str(item.replay_boundary_mode),
        "replayColumns": {
            "partition": item.driving_input_replay_columns.partition,
            "offset": item.driving_input_replay_columns.offset,
            "timestamp": item.driving_input_replay_columns.timestamp,
            "landed_at": item.driving_input_replay_columns.landed_at,
            "cursor": item.driving_input_replay_columns.cursor,
        },
        "propagatedModelNames": [key.name for key in item.propagated_model_keys],
        "hasAggregateSemantics": item.has_aggregate_semantics,
        "settings": dict(item.replay_settings),
        "rowsToReplay": rows_to_replay,
    }


def replay_time_column(*, boundary_mode: str, columns: AdapterReplayColumns) -> str:
    """The column the executor compares a forced start time against for this mode."""

    mode: ReplayLineageMode = ReplayLineageMode(boundary_mode)
    if mode in _TIMESTAMP_DRIVEN_MODES:
        return columns.timestamp
    if mode is ReplayLineageMode.OFFSETS:
        return columns.landed_at or columns.timestamp
    return columns.landed_at


def build_replay_count_query(
    *,
    database: str,
    relation_name: str,
    time_column: str,
    start_time: str | None,
) -> str:
    """Count the rows a replay of this root would read; unbounded for full replay."""

    base: str = f"SELECT count() AS rows FROM `{database}`.`{relation_name}`"
    if start_time is None:
        return base
    literal: str = _escape_time_literal(start_time)
    return f"{base} WHERE `{time_column}` >= toDateTime64('{literal}', 3, 'UTC')"


def count_replay_rows(
    *,
    connection: AdapterConnection,
    database: str,
    plan: DirectPlan,
    start_time: str | None,
) -> dict[str, int | None]:
    """Exact rows-to-replay per replay-root model; None when the anchor is unreadable."""

    counts: dict[str, int | None] = {}
    root: DirectReplayRoot
    for root in plan.replay_roots:
        counts[root.model_key.name] = _count_one_root(
            connection=connection,
            database=database,
            root=root,
            start_time=start_time,
        )
    return counts


def _count_one_root(
    *,
    connection: AdapterConnection,
    database: str,
    root: DirectReplayRoot,
    start_time: str | None,
) -> int | None:
    """One anchor count; a fresh project without the anchor table yields None, not an error."""

    query: str = build_replay_count_query(
        database=database,
        relation_name=root.driving_input_relation_name,
        time_column=replay_time_column(
            boundary_mode=str(root.replay_boundary_mode),
            columns=root.driving_input_replay_columns,
        ),
        start_time=start_time,
    )
    try:
        rows: tuple = connection.query(query).rows
    except AdapterError:
        return None
    if not rows:
        return None
    return int(str(rows[0][0]))


def _escape_time_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
