//! Shapes of deployment planning and execution: planner models, backfill, direct, population,
//! promotion and repair contracts, plus their value enums, exceptions and the neutral and
//! ClickHouse adapter constants.

use pyo3::exceptions::{PyLookupError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{IntoPyDict, PyFrozenSet, PyString, PyTuple};

use super::{
    Coerce, Given, add, add_class, base_init, exception, native_enum, path, shapes, str_enum,
};

const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const STAGED_SOURCE_NAME_PREFIX: &str = "_sqltide_next__";

/// The shared `AdapterDirectFingerprintSnapshot(status="absent", baselines=())` default, built
/// on first use because its class is still defined in Python.
static ABSENT_FINGERPRINTS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn absent_fingerprints(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let snapshot: &Py<PyAny> = ABSENT_FINGERPRINTS.get_or_try_init(py, || {
        let keywords = [
            ("status", PyString::new(py, "absent").into_any()),
            ("baselines", PyTuple::empty(py).into_any()),
        ]
        .into_py_dict(py)?;
        Ok::<_, PyErr>(
            py.import("sqltide.adapter.models")?
                .getattr("AdapterDirectFingerprintSnapshot")?
                .call((), Some(&keywords))?
                .unbind(),
        )
    })?;
    Ok(snapshot.bind(py).clone())
}

/// `str(uuid4())`, a fresh direct workflow identity.
#[pyfunction]
fn new_workflow_id(py: Python<'_>) -> PyResult<Bound<'_, PyString>> {
    py.import("uuid")?.getattr("uuid4")?.call0()?.str()
}

fn workflow_id_factory(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(wrap_pyfunction!(new_workflow_id, py)?.into_any())
}

/// `tuple(DirectResourceKind(kind) for kind in value)`.
fn resource_kinds<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = value.py();
    let kind: Bound<'py, PyAny> = native_enum(py, "DirectResourceKind")?;
    let mut kinds: Vec<Bound<'py, PyAny>> = Vec::new();
    for item in value.try_iter()? {
        kinds.push(kind.call1((item?,))?);
    }
    Ok(PyTuple::new(py, kinds)?.into_any())
}

/// `PublishExecutionError`, kept when the native module builds it.
static PUBLISH_EXECUTION_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn publish_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    let class: &Py<PyAny> = PUBLISH_EXECUTION_ERROR
        .get(py)
        .ok_or_else(|| PyValueError::new_err("PublishExecutionError is not built"))?;
    Ok(PyErr::from_value(class.bind(py).call1((message,))?))
}

/// `PublishOperation(value)`, raising `PublishExecutionError` from the `ValueError` it replaces.
fn publish_operation<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = value.py();
    match native_enum(py, "PublishOperation")?.call1((value,)) {
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            let raised: PyErr =
                publish_error(py, "publish operation must be 'promote' or 'rollback'")?;
            raised.set_cause(py, Some(error.clone_ref(py)));
            raised.value(py).setattr("__context__", error.value(py))?;
            Err(raised)
        }
        result => result,
    }
}

/// `RollbackRequest`: exactly one of a deployment ID and `previous` names the target.
fn rollback_target(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [deployment_id, previous, ..] = values else {
        return Err(PyValueError::new_err(
            "rollback request bound too few values",
        ));
    };
    if deployment_id.is_none() == !previous.is_truthy()? {
        return Err(publish_error(
            deployment_id.py(),
            "rollback requires either a deployment ID or --previous",
        )?);
    }
    Ok(())
}

shapes! {
    module = "sqltide.compiler.planner.models";

    /// Immutable live catalog and persisted state captured for one plan.
    PlanningWarehouseSnapshot { catalog, object_state_records }

    /// A normalized actual Kafka engine table.
    ActualKafkaTable { key, spec }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn columns(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["columns"])
        }

        #[getter]
        fn kafka(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["kafka"])
        }
    }

    /// A normalized actual managed ClickHouse table.
    ActualTable { key, spec }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn columns(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["columns"])
        }

        #[getter]
        fn engine(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "engine"])
        }

        #[getter]
        fn order_by(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "order_by"])
        }

        #[getter]
        fn partition_by(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "partition_by"])
        }

        #[getter]
        fn ttl(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "ttl"])
        }

        #[getter]
        fn settings(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["storage", "settings"])
        }
    }

    /// A normalized actual materialized view.
    ActualMaterializedView { key, spec }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn source_table_name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["source_table_name"])
        }

        #[getter]
        fn target_table_name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["target_table_name"])
        }

        #[getter]
        fn query(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["query"])
        }
    }

    /// A normalized actual ordinary view.
    ActualView { key, spec }
    @impl {
        #[getter]
        fn name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.key, &["name"])
        }

        #[getter]
        fn query(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.spec, &["query"])
        }
    }

    /// Project-level flat actual object graph.
    ActualState { objects }

    /// Inspection result for one managed root logical table.
    RootDeploymentInspection { root_key, state_kind, active_deployment_id }

    /// Row shape for persisted object-state metadata.
    ObjectStateMetadataRow {
        deployment_id,
        database_name,
        object_type,
        object_name,
        normalized_fingerprint,
        normalized_query,
        recorded_at,
        observation_id,
        state_kind,
    }

    /// A planner-local object change classification.
    PlannedObjectChange {
        key,
        change_type(coerce = Coerce::Enum("PlannedChangeType")),
        force_full_refresh(default = Given::Bool(false)),
        forced_start_time(default = Given::None),
        schema_change_kind(
            default = Given::None,
            coerce = Coerce::Maybe(&Coerce::Enum("TableSchemaChangeKind")),
        ),
        seed_compatibility(
            default = Given::None,
            coerce = Coerce::Maybe(&Coerce::Enum("TableSchemaSeedCompatibility")),
        ),
    }

    /// A unified SQL diff for one changed planned object.
    PlannedSqlDiff { key, object_type, name, diff_lines }

    /// A transitive desired-object rebuild subtree.
    RebuildSubtree {
        root_key,
        affected_keys,
        upstream_boundary_key,
        strategy(coerce = Coerce::Enum("RebuildStrategy")),
        replay_required(default = Given::Bool(true)),
        execution_mode(
            default = Given::Member("RebuildExecutionMode", "FULL_REBUILD"),
            coerce = Coerce::Enum("RebuildExecutionMode"),
        ),
        forced_full_refresh(default = Given::Bool(false)),
        forced_start_time(default = Given::None),
        requested_start_time(default = Given::None),
        configured_backfill_mode(
            default = Given::None,
            coerce = Coerce::Maybe(&Coerce::Enum("ReplayOnChangeMode")),
        ),
        execution_lookback_seconds(default = Given::None),
        history_preserving_bounded_supported(default = Given::Bool(true)),
        resolved_bounded_replay_fallback(
            default = Given::None,
            coerce = Coerce::Maybe(&Coerce::Enum("BoundedReplayFallback")),
        ),
    }

    /// A staged deployment step for a rebuild plan.
    DeploymentStep {
        step_id,
        phase(coerce = Coerce::Enum("DeploymentPhase")),
        action(coerce = Coerce::Enum("DeploymentAction")),
        root_key,
        target_key(default = Given::None),
        physical_name(default = Given::None),
    }

    /// A deterministic physical shadow-object identity for a logical object.
    PreparedShadowObject { logical_key, physical_name, logical_model_name }

    /// A planner-visible warning about rollout semantics.
    PlannerWarning { warning_code, message, root_key, target_key(default = Given::None) }

    /// A conservative staged deployment plan.
    DeploymentPlan {
        deployment_id,
        object_changes,
        rebuild_subtrees,
        steps,
        prepared_shadow_objects,
        warnings,
        sql_diffs(default = Given::EmptyTuple),
    }

    /// A logical-to-physical prepared object mapping for a deployment.
    PreparedObjectMapping { logical_key, physical_name, logical_model_name }

    /// Framework-owned applied state for a logical object.
    ObjectStateRecord {
        deployment_id,
        key,
        normalized_fingerprint,
        normalized_query,
        recorded_at,
        observation_id(default = Given::Str("")),
        state_kind(default = Given::Str("deployment")),
    }

    /// Stored staged deployment metadata.
    DeploymentRecord {
        deployment_id,
        created_at,
        status,
        replay_lineage_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        selected_root_keys,
        warning_codes,
        prepared_object_mappings,
        workflow_fingerprint(default = Given::Str("")),
        boundary_time(default = Given::None),
        tool_version(default = Given::Str("")),
    }

    /// Stored replay-boundary or progress metadata for a deployment.
    DeploymentWatermarkRecord { deployment_id, root_key, anchor_key, boundary_key, cutoff_value }

    /// Stored publish/activation history for one deployment.
    PublishEventRecord {
        deployment_id,
        published_at,
        logical_view_names,
        database(default = Given::Str("")),
        physical_relation_names(default = Given::EmptyTuple),
    }

    /// Project-level stored metadata-state records.
    MetadataState { object_states, deployments, deployment_watermarks, publish_events }

    /// Immutable live catalog captured for one Direct plan.
    DirectWarehouseSnapshot { catalog, fingerprints(default = Given::Lazy(absent_fingerprints)) }

    /// One logical model the direct plan will tear down and rebuild.
    DirectPlanEntry {
        model_key,
        reason(coerce = Coerce::Enum("DirectPlanReason")),
        relation_names,
        resource_kinds(coerce = Coerce::With(resource_kinds)),
        driving_input_key,
        is_replay_root,
        sql_change(default = Given::None),
    }

    /// Optional logical SQL baseline comparison for one selected direct model.
    DirectSqlChange {
        status(coerce = Coerce::Enum("DirectSqlBaselineStatus")),
        current_sql,
        current_hash,
        previous_sql,
        previous_hash,
        unified_diff,
        warning(default = Given::None),
    }

    /// One upstream resource that must exist but is never executed.
    DirectPrerequisite {
        key,
        relation_names,
        present,
        framework_managed(default = Given::Bool(false)),
    }

    /// One executed model replayed from its own preserved driving input.
    DirectReplayRoot {
        model_key,
        driving_input_key,
        driving_input_relation_name,
        driving_input_replay_columns,
        replay_boundary_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        propagated_model_keys,
        has_aggregate_semantics(default = Given::Bool(false)),
        replay_settings(default = Given::EmptyTuple),
    }

    /// One destructive or constructive relation action in dependency-safe order.
    DirectRelationOperation {
        relation_name,
        action(coerce = Coerce::Enum("DirectRelationAction")),
        model_key,
        resource_kind(coerce = Coerce::Enum("DirectResourceKind")),
    }

    /// One owning pipeline a changed selection rebuilds so a drifted managed source is recreated.
    DirectSourceDriftSelection {
        source_key,
        pipeline_name,
        reader_pipeline_names,
        drifted_setting_names,
        model_keys,
    }

    /// A view-only switch of one live model's row-error policy, without replay.
    DirectRowErrorPolicyChange {
        model_key,
        view_name,
        dead_letter_view_name,
        desired_policy,
        landing_view_names,
        logical_view_name(default = Given::Str("")),
        logical_dead_letter_view_name(default = Given::Str("")),
        previous_view_definition(default = Given::None),
        landing_repair_only(default = Given::Bool(false)),
    }

    /// One deterministic direct-mode execution plan for a selected closure.
    DirectPlan {
        database,
        user_scope,
        execution_scope,
        prerequisite_scope,
        entries,
        replay_roots,
        teardown_operations,
        creation_operations,
        selection_mode(
            default = Given::Member("DirectSelectionMode", "EXPLICIT"),
            coerce = Coerce::Enum("DirectSelectionMode"),
        ),
        prerequisite_execution_scope(default = Given::EmptyTuple),
        warnings(default = Given::EmptyTuple),
        effective_start_time(default = Given::None),
        source_drift_selections(default = Given::EmptyTuple),
    }
}

shapes! {
    module = "sqltide.executor.backfill.models";

    /// Input required to bootstrap a staged backfill deployment.
    BackfillBootstrapRequest {
        desired_state,
        default_database,
        metadata_database,
        replay_lineage_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        confirmed_plan(default = Given::None),
        deployment_id(default = Given::None),
        full_refresh_keys(default = Given::EmptyFrozenSet),
        start_time_keys(default = Given::EmptyFrozenSet),
        start_time(default = Given::None),
        created_at(default = Given::None),
        boundary_time(default = Given::None),
        stabilization_seconds(default = Given::Float(5.0)),
        confirmed_target_catalog(default = Given::None),
        confirmed_metadata_catalog(default = Given::None),
        row_error_policy_changes(default = Given::EmptyTuple),
    }

    /// Result of bootstrapping a staged backfill deployment.
    BackfillBootstrapResult {
        deployment_id,
        created_at,
        deployment_plan,
        root_reports,
        existing_relation_names,
    }

    /// One staged replay root actually executed in the warehouse.
    BackfillRootReplayResult { root_key, written_rows }

    /// Result of executing a staged backfill through boundary capture and replay.
    BackfillExecutionResult { bootstrap, boundary_time, replay_results }

    /// Deployment identity fixed before a virtual build is confirmed.
    BackfillDeploymentIdentity { deployment_id, created_at }

    /// User-facing rebuild strategy report for one managed root.
    RootBackfillReport { root_key, state_kind, replay_strategy, active_deployment_id }
}

shapes! {
    module = "sqltide.executor.direct.models";

    /// One complete instruction to realize a planned direct closure.
    DirectBuildRequest {
        plan,
        realized_project,
        database,
        metadata_database,
        tool_version,
        workflow_id(default = Given::Factory(workflow_id_factory)),
        stabilization_seconds(default = Given::Float(5.0)),
        boundary_time(default = Given::None),
        audits(default = Given::EmptyTuple),
        row_error_policy_changes(default = Given::EmptyTuple),
    }
    @impl {
        /// Return the normalized lower bound carried by the confirmed plan.
        #[getter]
        fn effective_start_time(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.plan, &["effective_start_time"])
        }
    }

    /// One deployed managed source whose Kafka settings differ from the project declaration.
    DriftedManagedSource { source_key, owner_pipeline_names, drifted_setting_names }

    /// One selected direct audit with refs resolved before workflow assembly.
    DirectBuildAudit { name, query, severity, description }

    /// One retained interval captured for a direct replay root.
    DirectReplayRange {
        partition_value,
        source_partition_column_name,
        source_position_column_name,
        source_timestamp_column_name,
        lower_value,
        upper_value,
        replay_cutoff_value,
        cutoff_inclusive,
        anchor_row_count(default = Given::None),
    }

    /// Process-owned replay boundaries captured after live stabilization.
    DirectReplayCapture {
        capture_id,
        workflow_id,
        target_database,
        logical_model_name,
        driving_input_relation_name,
        boundary_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        captured_at,
        ranges,
    }

    /// One replay template and the steps that capture and consume its boundary.
    DirectRuntimeReplay {
        model_name,
        capture_step_id,
        replay_step_id,
        replay,
        boundary_column_type,
        managed_landing_anchor(default = Given::Bool(false)),
    }

    /// A direct workflow template plus its runtime replay realization inputs.
    DirectBuildWorkflow {
        template,
        runtime_replays,
        workflow_id,
        source_recovery_statements(default = Given::EmptyTuple),
        recreated_sources(default = Given::EmptyTuple),
    }
    @impl {
        #[getter]
        fn mode(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.template, &["mode"])
        }

        #[getter]
        fn plan_json(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.template, &["plan_json"])
        }

        #[getter]
        fn statements(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.template, &["statements"])
        }
    }

    /// Exact direct workflow and typed captures produced by one runtime execution.
    DirectRuntimeExecution { workflow, execution, captures }

    /// One partition-scoped or scalar cutoff separating replay from live propagation.
    DirectReplayBoundary {
        model_name,
        driving_input_relation_name,
        replay_boundary_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        boundary_key,
        cutoff_value,
        cutoff_inclusive,
    }

    /// One direct replay root actually executed in the warehouse.
    DirectRootReplayResult { model_name, written_rows }

    /// Everything one direct build durably changed, in execution order.
    DirectBuildResult {
        database,
        preserved_source_relation_names,
        created_source_relation_names,
        dropped_relation_names,
        created_relation_names,
        boundary_time,
        boundaries,
        replay_results,
        effective_start_time(default = Given::None),
    }

    /// Direct build and audit evidence decoded from one workflow execution.
    DirectBuildExecutionResult { build_result, audit_result }
}

shapes! {
    module = "sqltide.executor.population.models";

    /// One replay root and its physical propagation scope.
    PopulationRoot {
        root_key,
        affected_keys,
        upstream_boundary_key,
        replay_lineage_mode(coerce = Coerce::Enum("ReplayLineageMode")),
        execution_mode(
            default = Given::Member("RebuildExecutionMode", "FULL_REBUILD"),
            coerce = Coerce::Enum("RebuildExecutionMode"),
        ),
        forced_start_time(default = Given::None),
        execution_lookback_seconds(default = Given::None),
        persist_watermarks(default = Given::Bool(true)),
    }

    /// One logical object mapped to its execution-time physical name.
    PopulationObject { logical_key, physical_name }

    /// The complete mode-neutral physical population instruction.
    PopulationPlan { execution_id, roots, objects }

    /// One inclusive replay cutoff for a population root.
    PopulationWatermark { root_key, anchor_key, boundary_key, cutoff_value }

    /// One complete managed-source definition used during source preparation.
    PopulationManagedSource { resource, database }

    /// One population resource ready for render-only warehouse realization.
    PopulationRealization { resource, database }

    /// One managed source whose drifted settings are reset while its landing table is kept.
    PopulationSourceRecreation {
        managed_source,
        landing_view_name,
        landing_relation_name,
        consumer_group,
        drifted_setting_names,
    }
    @impl {
        /// Name the declared Kafka table staged beside the drifted one before the swap.
        #[getter]
        fn staged_relation_name(&self, py: Python<'_>) -> PyResult<String> {
            let name: Py<PyAny> = path(py, &self.managed_source, &["resource", "name"])?;
            Ok(format!("{STAGED_SOURCE_NAME_PREFIX}{}", name.bind(py).str()?))
        }
    }

    /// Managed source resources preserved, created, recreated, and awaiting activation.
    PopulationSourcePreparation {
        preserved_relation_names,
        created_relation_names,
        landing_views,
        managed_sources,
        recreated_sources(default = Given::EmptyTuple),
    }
}

shapes! {
    module = "sqltide.executor.promotion.models";

    /// Input required to publish a staged deployment.
    PublishRequest {
        deployment_id,
        metadata_database,
        default_database,
        operation(
            default = Given::Member("PublishOperation", "PROMOTE"),
            coerce = Coerce::With(publish_operation),
        ),
        previous_deployment_id(default = Given::None),
    }

    /// A stable logical view created or replaced during publish.
    PublishedView { view_name, target_table_name }

    /// Result of publishing a staged deployment.
    PublishResult {
        deployment_id,
        published_views,
        per_relation_atomic_replace,
        graph_atomic_publish,
        operation(
            default = Given::Member("PublishOperation", "PROMOTE"),
            coerce = Coerce::Enum("PublishOperation"),
        ),
        previous_deployment_id(default = Given::None),
    }

    /// One new stable logical binding created by promotion.
    PromotionBindingAddition { database, logical_name, physical_name }

    /// One stable logical binding switched to a different physical relation.
    PromotionBindingReplacement { database, logical_name, from_physical_name, to_physical_name }

    /// One obsolete live binding removed by promotion.
    PromotionBindingRemoval { database, logical_name, physical_name }

    /// One formerly live physical relation left unbound after promotion.
    PromotionOrphanedRelation { database, physical_name }

    /// Exact live-binding effects of one deployment promotion.
    DeploymentPromotionPreview {
        classification,
        additions,
        replacements,
        removals,
        orphaned_relations,
    }

    /// Input used to resolve one whole-deployment rollback.
    RollbackRequest { deployment_id, previous, metadata_database, default_database }
    @post_init(rollback_target)

    /// Resolved current and target publication identities for rollback.
    RollbackPlan { current_deployment_id, target_deployment_id, logical_view_names }
}

shapes! {
    module = "sqltide.executor.repair.models";

    /// Input required to repair a stable active view.
    RepairActiveViewRequest { default_database, table_name, deployment_id }

    /// Result of explicitly rebinding a stable active view.
    RepairActiveViewResult { table_name, target_table_name }
}

#[pyfunction]
#[pyo3(
    name = "__init__",
    signature = (instance, *, workflow, partial_result, captures, failed_step_id, cause)
)]
fn direct_workflow_execution_init<'py>(
    instance: &Bound<'py, PyAny>,
    workflow: Bound<'py, PyAny>,
    partial_result: Bound<'py, PyAny>,
    captures: Bound<'py, PyAny>,
    failed_step_id: Bound<'py, PyAny>,
    cause: Bound<'py, PyAny>,
) -> PyResult<()> {
    base_init(instance, [cause.str()?.into_any()])?;
    instance.setattr("workflow", workflow)?;
    instance.setattr("partial_result", partial_result)?;
    instance.setattr("captures", captures)?;
    instance.setattr("failed_step_id", failed_step_id)?;
    instance.setattr("cause", cause)
}

/// A native enum: its class name, docstring and members.
type EnumSpec = (
    &'static str,
    Option<&'static str>,
    &'static [(&'static str, &'static str)],
);

/// The planner value enums of `sqltide.compiler.planner.types`.
const PLANNER_ENUMS: [EnumSpec; 13] = [
    (
        "PlannedChangeType",
        None,
        &[
            ("REBUILD", "rebuild"),
            ("CREATE", "create"),
            ("REPLACE", "replace"),
            ("NO_OP", "no_op"),
        ],
    ),
    (
        "TableSchemaChangeKind",
        None,
        &[("NON_BREAKING", "non_breaking"), ("BREAKING", "breaking")],
    ),
    (
        "TableSchemaSeedCompatibility",
        None,
        &[("SEEDABLE", "seedable"), ("NON_SEEDABLE", "non_seedable")],
    ),
    (
        "RebuildStrategy",
        None,
        &[
            ("SHADOW_REBUILD", "shadow_rebuild"),
            ("OFFLINE_REBUILD", "offline_rebuild"),
        ],
    ),
    (
        "RebuildExecutionMode",
        None,
        &[
            ("FULL_REBUILD", "full_rebuild"),
            ("SEEDED_BOUNDED_REBUILD", "seeded_bounded_rebuild"),
            ("UNSEEDED_BOUNDED_REBUILD", "unseeded_bounded_rebuild"),
        ],
    ),
    (
        "DeploymentPhase",
        None,
        &[
            ("PLAN", "plan"),
            ("BACKFILL", "backfill"),
            ("AUDIT", "audit"),
            ("PUBLISH", "publish"),
        ],
    ),
    (
        "DeploymentAction",
        None,
        &[
            ("PLAN_SHADOW_TABLE", "plan_shadow_table"),
            (
                "PLAN_SHADOW_MATERIALIZED_VIEW",
                "plan_shadow_materialized_view",
            ),
            ("PLAN_SHADOW_VIEW", "plan_shadow_view"),
            ("BACKFILL_SUBTREE", "backfill_subtree"),
            ("AUDIT_SUBTREE", "audit_subtree"),
            ("PUBLISH_SUBTREE", "publish_subtree"),
        ],
    ),
    (
        "RootDeploymentStateKind",
        Some("How one managed root presents itself in the warehouse."),
        &[
            ("ACTIVE_VIEW_PRESENT", "active_view_present"),
            ("GREENFIELD", "greenfield"),
            ("LOGICAL_VIEW_MISSING", "logical_view_missing"),
            ("INVALID_ACTIVE_VIEW", "invalid_active_view"),
        ],
    ),
    (
        "DirectPlanReason",
        Some("Why one logical model is in the direct execution scope."),
        &[
            ("SELECTED", "selected"),
            ("CHANGED", "changed"),
            ("SOURCE_DRIFT", "source_drift"),
            ("DOWNSTREAM_OF_SELECTED", "downstream_of_selected"),
            ("MISSING_UPSTREAM", "missing_upstream"),
            ("ALL_MODELS", "all_models"),
        ],
    ),
    (
        "DirectSelectionMode",
        Some("How the roots of one direct plan were selected."),
        &[
            ("ALL_MODELS", "all_models"),
            ("EXPLICIT", "explicit"),
            ("CHANGED", "changed"),
        ],
    ),
    (
        "DirectSqlBaselineStatus",
        Some("Relationship between current logical SQL and its optional applied baseline."),
        &[
            ("FIRST_BASELINE", "first_baseline"),
            ("QUERY_CHANGED", "query_changed"),
            ("NO_QUERY_CHANGE", "no_query_change"),
            ("BASELINE_UNAVAILABLE", "baseline_unavailable"),
        ],
    ),
    (
        "DirectRelationAction",
        Some("One destructive or constructive relation action in a direct plan."),
        &[("DROP", "drop"), ("CREATE", "create")],
    ),
    (
        "DirectResourceKind",
        Some("The physical resource kind of one direct relation."),
        &[
            ("TABLE", "table"),
            ("MATERIALIZED_VIEW", "materialized_view"),
            ("VIEW", "view"),
        ],
    ),
];

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    for (name, doc, members) in PLANNER_ENUMS {
        str_enum(module, name, PLANNER_TYPES, name, doc, members)?;
    }
    str_enum(
        module,
        "PublishOperation",
        "sqltide.executor.promotion.types",
        "PublishOperation",
        Some("Reason stable bindings were published."),
        &[("PROMOTE", "promote"), ("ROLLBACK", "rollback")],
    )?;
    str_enum(
        module,
        "PromotionPreviewClassification",
        "sqltide.executor.promotion.types",
        "PromotionPreviewClassification",
        Some("Conservative UI classification for a binding replacement request."),
        &[
            ("INITIAL_PUBLISH", "initial_publish"),
            ("PROMOTION", "promotion"),
        ],
    )
}

fn exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let value_error: Bound<'_, PyAny> = py.get_type::<PyValueError>().into_any();
    let plain: [(&str, &str, &str); 5] = [
        (
            PLANNER_EXCEPTIONS,
            "DeploymentPlanError",
            "Raised when planner input or state is invalid.",
        ),
        (
            PLANNER_EXCEPTIONS,
            "ActualStateError",
            "Raised when actual-state input or state is invalid.",
        ),
        (
            PLANNER_EXCEPTIONS,
            "DirectPlanError",
            "Raised when a direct-mode plan cannot be built safely.",
        ),
        (
            "sqltide.executor.backfill.exceptions",
            "BackfillExecutionError",
            "Raised when backfill input or state is invalid.",
        ),
        (
            "sqltide.executor.promotion.exceptions",
            "PublishExecutionError",
            "Raised when publish input or state is invalid.",
        ),
    ];
    for (public_module, name, doc) in plain {
        let class: Bound<'_, PyAny> = exception(
            module,
            public_module,
            name,
            doc,
            std::slice::from_ref(&value_error),
            None,
            None,
        )?;
        if name == "PublishExecutionError" {
            PUBLISH_EXECUTION_ERROR.get_or_init(py, || class.unbind());
        }
    }
    let direct: Bound<'_, PyAny> = exception(
        module,
        "sqltide.executor.direct.exceptions",
        "DirectBuildError",
        "Raised when a direct build cannot proceed safely.",
        &[py.get_type::<PyRuntimeError>().into_any()],
        None,
        None,
    )?;
    exception(
        module,
        "sqltide.executor.direct.exceptions",
        "DirectWorkflowExecutionError",
        "Raised with exact attempted SQL when a direct runtime workflow fails.",
        std::slice::from_ref(&direct),
        Some(wrap_pyfunction!(direct_workflow_execution_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        "sqltide.executor.population.exceptions",
        "PopulationScopeError",
        "Raised when a population plan names an object its desired state lacks.",
        &[py.get_type::<PyLookupError>().into_any()],
        None,
        None,
    )?;
    Ok(())
}

/// `frozenset(AdapterReplayBoundaryMode)`, built on first read because the enum is still
/// defined in Python.
pub(super) fn clickhouse_replay_boundary_modes(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let modes: Bound<'_, PyAny> = py
        .import("sqltide.adapter.types")?
        .getattr("AdapterReplayBoundaryMode")?;
    py.get_type::<PyFrozenSet>().call1((modes,))
}

const PLANNER_MEMBERS: [(&str, &str, &str); 12] = [
    (
        "PLANNED_CHANGE_TYPE_REBUILD",
        "PlannedChangeType",
        "REBUILD",
    ),
    ("PLANNED_CHANGE_TYPE_CREATE", "PlannedChangeType", "CREATE"),
    ("PLANNED_CHANGE_TYPE_NO_OP", "PlannedChangeType", "NO_OP"),
    (
        "TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING",
        "TableSchemaChangeKind",
        "NON_BREAKING",
    ),
    (
        "TABLE_SCHEMA_CHANGE_KIND_BREAKING",
        "TableSchemaChangeKind",
        "BREAKING",
    ),
    (
        "TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE",
        "TableSchemaSeedCompatibility",
        "SEEDABLE",
    ),
    (
        "TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE",
        "TableSchemaSeedCompatibility",
        "NON_SEEDABLE",
    ),
    (
        "REBUILD_STRATEGY_SHADOW",
        "RebuildStrategy",
        "SHADOW_REBUILD",
    ),
    (
        "REBUILD_EXECUTION_MODE_FULL",
        "RebuildExecutionMode",
        "FULL_REBUILD",
    ),
    (
        "REBUILD_EXECUTION_MODE_SEEDED_BOUNDED",
        "RebuildExecutionMode",
        "SEEDED_BOUNDED_REBUILD",
    ),
    (
        "REBUILD_EXECUTION_MODE_UNSEEDED_BOUNDED",
        "RebuildExecutionMode",
        "UNSEEDED_BOUNDED_REBUILD",
    ),
    ("DEPLOYMENT_PHASE_PLAN", "DeploymentPhase", "PLAN"),
];

const ADAPTER_TEXT: [(&str, &str); 24] = [
    ("REDACTED_SECRET_PLACEHOLDER", "***"),
    (
        "ADAPTER_DATABASE_PLACEHOLDER",
        "__sqltide_target_database__",
    ),
    (
        "METADATA_OBJECT_STATE_TABLE_NAME",
        "_sqltide_virtual_object_state",
    ),
    (
        "METADATA_DEPLOYMENTS_TABLE_NAME",
        "_sqltide_virtual_deployments",
    ),
    (
        "METADATA_DEPLOYMENT_WATERMARKS_TABLE_NAME",
        "_sqltide_virtual_replay_boundaries",
    ),
    (
        "METADATA_PUBLISH_HISTORY_TABLE_NAME",
        "_sqltide_virtual_publications",
    ),
    (
        "METADATA_SCHEMA_VERSIONS_TABLE_NAME",
        "_sqltide_schema_versions",
    ),
    (
        "METADATA_DIRECT_FINGERPRINTS_TABLE_NAME",
        "_sqltide_direct_fingerprints",
    ),
    ("METADATA_MANIFESTS_TABLE_NAME", "_sqltide_manifests"),
    ("METADATA_DEAD_LETTERS_TABLE_NAME", "_sqltide_dead_letters"),
    ("QUARANTINE_LIVE_VIEW_PATTERN", r"\bAS __sqt_value_0\b"),
    ("METADATA_INVOCATIONS_TABLE_NAME", "_sqltide_invocations"),
    ("METADATA_NODE_RESULTS_TABLE_NAME", "_sqltide_node_results"),
    ("METADATA_RUN_EVENTS_TABLE_NAME", "_sqltide_run_events"),
    (
        "METADATA_RUN_STATEMENTS_TABLE_NAME",
        "_sqltide_run_statements",
    ),
    (
        "METADATA_SENSOR_CHECKPOINTS_TABLE_NAME",
        "_sqltide_sensor_checkpoints",
    ),
    ("METADATA_SENSOR_TICKS_TABLE_NAME", "_sqltide_sensor_ticks"),
    ("METADATA_SENSOR_STEPS_TABLE_NAME", "_sqltide_sensor_steps"),
    (
        "METADATA_SENSOR_OVERRIDES_TABLE_NAME",
        "_sqltide_sensor_overrides",
    ),
    (
        "METADATA_SENSOR_LEASES_TABLE_NAME",
        "_sqltide_sensor_leases",
    ),
    ("VIRTUAL_OBJECT_STATE_KIND_DEPLOYMENT", "deployment"),
    ("VIRTUAL_OBJECT_STATE_KIND_RECONCILE", "reconcile"),
    ("VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE", "incomplete"),
    ("MANAGED_SOURCE_KIND_KAFKA", "kafka"),
];

const CLICKHOUSE_FLAGS: [(&str, bool); 7] = [
    ("CLICKHOUSE_VIRTUAL_ENVIRONMENTS_SUPPORTED", true),
    ("CLICKHOUSE_HISTORY_PREFIX_SEED_SUPPORTED", true),
    ("CLICKHOUSE_STABLE_LOGICAL_BINDINGS_SUPPORTED", true),
    ("CLICKHOUSE_PER_RELATION_ATOMIC_REPLACE", true),
    ("CLICKHOUSE_GRAPH_ATOMIC_PUBLISH", false),
    ("CLICKHOUSE_SET_DIFFERENCE_COMPARISON_SUPPORTED", true),
    ("CLICKHOUSE_DIRECT_REBUILD_SUPPORTED", true),
];

fn constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    for (name, class, member) in PLANNER_MEMBERS {
        add(module, name, native_enum(py, class)?.getattr(member)?)?;
    }
    add(
        module,
        "DEPLOYMENT_ACTION_PLAN_SHADOW_TABLE",
        native_enum(py, "DeploymentAction")?.getattr("PLAN_SHADOW_TABLE")?,
    )?;
    add(module, "BLANK_VALUES", (py.None(), ""))?;
    add(module, "DIRECT_DEAD_LETTERS_VIEW_SUFFIX", "__dead_letters")?;
    for (name, value) in ADAPTER_TEXT {
        add(module, name, value)?;
    }
    add(
        module,
        "ADAPTER_SECRET_PLACEHOLDER_PREFIX",
        "__sqltide_secret__",
    )?;
    add(module, "ADAPTER_SECRET_PLACEHOLDER_SUFFIX", "__")?;
    add(module, "CLICKHOUSE_ADAPTER_NAME", "clickhouse")?;
    add(
        module,
        "CLICKHOUSE_WRITTEN_ROWS_SUMMARY_KEY",
        "written_rows",
    )?;
    add(module, "CLICKHOUSE_DEFAULT_DATABASE", "default")?;
    add(module, "CLICKHOUSE_SQL_ANALYSIS_DIALECT", "clickhouse")?;
    for (name, value) in CLICKHOUSE_FLAGS {
        add(module, name, value)?;
    }
    add(
        module,
        "CLICKHOUSE_MANAGED_SOURCE_KINDS",
        PyFrozenSet::new(py, ["kafka"])?,
    )?;
    add(module, "CLICKHOUSE_CATALOG_RETRY_DELAY_SECONDS", 0.05)?;
    add(module, "CLICKHOUSE_MODEL_TABLE_NAME_PREFIX", "tbl__")?;
    add(
        module,
        "STAGED_SOURCE_NAME_PREFIX",
        STAGED_SOURCE_NAME_PREFIX,
    )
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    exceptions(module)?;
    constants(module)?;
    add_class::<PlanningWarehouseSnapshot>(module)?;
    add_class::<ActualKafkaTable>(module)?;
    add_class::<ActualTable>(module)?;
    add_class::<ActualMaterializedView>(module)?;
    add_class::<ActualView>(module)?;
    add_class::<ActualState>(module)?;
    add_class::<RootDeploymentInspection>(module)?;
    add_class::<ObjectStateMetadataRow>(module)?;
    add_class::<PlannedObjectChange>(module)?;
    add_class::<PlannedSqlDiff>(module)?;
    add_class::<RebuildSubtree>(module)?;
    add_class::<DeploymentStep>(module)?;
    add_class::<PreparedShadowObject>(module)?;
    add_class::<PlannerWarning>(module)?;
    add_class::<DeploymentPlan>(module)?;
    add_class::<PreparedObjectMapping>(module)?;
    add_class::<ObjectStateRecord>(module)?;
    add_class::<DeploymentRecord>(module)?;
    add_class::<DeploymentWatermarkRecord>(module)?;
    add_class::<PublishEventRecord>(module)?;
    add_class::<MetadataState>(module)?;
    add_class::<DirectWarehouseSnapshot>(module)?;
    add_class::<DirectPlanEntry>(module)?;
    add_class::<DirectSqlChange>(module)?;
    add_class::<DirectPrerequisite>(module)?;
    add_class::<DirectReplayRoot>(module)?;
    add_class::<DirectRelationOperation>(module)?;
    add_class::<DirectSourceDriftSelection>(module)?;
    add_class::<DirectRowErrorPolicyChange>(module)?;
    add_class::<DirectPlan>(module)?;
    add_class::<BackfillBootstrapRequest>(module)?;
    add_class::<BackfillBootstrapResult>(module)?;
    add_class::<BackfillRootReplayResult>(module)?;
    add_class::<BackfillExecutionResult>(module)?;
    add_class::<BackfillDeploymentIdentity>(module)?;
    add_class::<RootBackfillReport>(module)?;
    add_class::<DirectBuildRequest>(module)?;
    add_class::<DriftedManagedSource>(module)?;
    add_class::<DirectBuildAudit>(module)?;
    add_class::<DirectReplayRange>(module)?;
    add_class::<DirectReplayCapture>(module)?;
    add_class::<DirectRuntimeReplay>(module)?;
    add_class::<DirectBuildWorkflow>(module)?;
    add_class::<DirectRuntimeExecution>(module)?;
    add_class::<DirectReplayBoundary>(module)?;
    add_class::<DirectRootReplayResult>(module)?;
    add_class::<DirectBuildResult>(module)?;
    add_class::<DirectBuildExecutionResult>(module)?;
    add_class::<PopulationRoot>(module)?;
    add_class::<PopulationObject>(module)?;
    add_class::<PopulationPlan>(module)?;
    add_class::<PopulationWatermark>(module)?;
    add_class::<PopulationManagedSource>(module)?;
    add_class::<PopulationRealization>(module)?;
    add_class::<PopulationSourceRecreation>(module)?;
    add_class::<PopulationSourcePreparation>(module)?;
    add_class::<PublishRequest>(module)?;
    add_class::<PublishedView>(module)?;
    add_class::<PublishResult>(module)?;
    add_class::<PromotionBindingAddition>(module)?;
    add_class::<PromotionBindingReplacement>(module)?;
    add_class::<PromotionBindingRemoval>(module)?;
    add_class::<PromotionOrphanedRelation>(module)?;
    add_class::<DeploymentPromotionPreview>(module)?;
    add_class::<RollbackRequest>(module)?;
    add_class::<RollbackPlan>(module)?;
    add_class::<RepairActiveViewRequest>(module)?;
    add_class::<RepairActiveViewResult>(module)
}
