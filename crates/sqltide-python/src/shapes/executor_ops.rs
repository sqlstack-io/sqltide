//! Shapes of the operational executor commands: destruction, readiness audits, deployment
//! inventory and diffs, SQL audits, run observability, janitor, reconcile, SQL tests and doctor,
//! plus their value enums, exceptions and constants.

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{IntoPyDict, PyFrozenSet, PySet, PyTuple};

use super::{
    Coerce, Given, add, add_class, base_init, exception, factory, native_enum, raised, shapes,
    str_enum,
};

const DESTRUCTION_EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const REPLAY_LINEAGE_MODE: Coerce = Coerce::Maybe(&Coerce::Enum("ReplayLineageMode"));

/// `DestructionValidationError(message)`.
fn invalid_destruction(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    raised(py, "DestructionValidationError", message.to_owned())
}

/// `tuple(sorted(value))`.
fn sorted_tuple<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = value.py();
    let sorted: Bound<'py, PyAny> = py.import("builtins")?.getattr("sorted")?.call1((value,))?;
    py.get_type::<PyTuple>().call1((sorted,))
}

/// `DestructionRequest`: orphans only with pipeline destruction, unique and disjoint pipeline
/// selections, each then kept sorted and deduplicated.
fn destruction_request(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [
        operation,
        _,
        _,
        _,
        pipeline_names,
        included,
        include_orphans,
    ] = values
    else {
        return Err(PyValueError::new_err(
            "destruction request bound the wrong number of values",
        ));
    };
    let py: Python<'_> = operation.py();
    let set: Bound<'_, PyAny> = py.get_type::<PySet>().into_any();
    if include_orphans.is_truthy()?
        && operation.ne(native_enum(py, "DestructionOperation")?.getattr("DESTROY_PIPELINES")?)?
    {
        return Err(invalid_destruction(
            py,
            "Orphan inclusion is only valid for selected pipeline destruction",
        )?);
    }
    if set.call1((&*pipeline_names,))?.len()? != pipeline_names.len()? {
        return Err(invalid_destruction(
            py,
            "Pipeline destruction selection contains duplicate names",
        )?);
    }
    if set.call1((&*included,))?.len()? != included.len()? {
        return Err(invalid_destruction(
            py,
            "Included dependent pipeline selection contains duplicate names",
        )?);
    }
    let overlap: Bound<'_, PyAny> = set
        .call1((&*pipeline_names,))?
        .bitand(set.call1((&*included,))?)?;
    if overlap.is_truthy()? {
        return Err(raised(
            py,
            "DestructionValidationError",
            format!(
                "Pipelines cannot be both originally selected and included dependants: {}",
                sorted_tuple(&overlap)?.repr()?
            ),
        )?);
    }
    *pipeline_names = sorted_tuple(&set.call1((&*pipeline_names,))?)?;
    *included = sorted_tuple(&set.call1((&*included,))?)?;
    Ok(())
}

/// `JanitorRequest`: retention and the kept rollback deployments are non-negative.
fn janitor_request(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [_, _, retention_days, _, minimum_rollback_deployments] = values else {
        return Err(PyValueError::new_err(
            "janitor request bound the wrong number of values",
        ));
    };
    let py: Python<'_> = retention_days.py();
    if retention_days.lt(0)? {
        return Err(raised(
            py,
            "JanitorInputError",
            "retention_days must be non-negative".to_owned(),
        )?);
    }
    if minimum_rollback_deployments.lt(0)? {
        return Err(raised(
            py,
            "JanitorInputError",
            "minimum_rollback_deployments must be non-negative".to_owned(),
        )?);
    }
    Ok(())
}

/// `relation.total_bytes or 0`, one relation's share of a plan's estimated bytes.
#[pyfunction]
fn relation_bytes<'py>(relation: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let total: Bound<'py, PyAny> = relation.getattr("total_bytes")?;
    if total.is_truthy()? {
        return Ok(total);
    }
    Ok(0_i64.into_pyobject(relation.py())?.into_any())
}

/// How many audits failed at `severity` without being deferred.
fn failure_count(results: &Bound<'_, PyAny>, severity: &str) -> PyResult<usize> {
    let py: Python<'_> = results.py();
    let wanted: Bound<'_, PyAny> = native_enum(py, "AuditSeverity")?.getattr(severity)?;
    let mut count: usize = 0;
    for result in results.try_iter()? {
        let result: Bound<'_, PyAny> = result?;
        if result.getattr("severity")?.eq(&wanted)?
            && !result.getattr("passed")?.is_truthy()?
            && !result.getattr("deferred")?.is_truthy()?
        {
            count += 1;
        }
    }
    Ok(count)
}

shapes! {
    module = "sqltide.executor.destruction.models";

    DestructionRequest {
        operation(coerce = Coerce::Enum("DestructionOperation")),
        target,
        database,
        metadata_database,
        pipeline_names(default = Given::EmptyTuple),
        included_dependent_pipeline_names(default = Given::EmptyTuple),
        include_orphans(default = Given::Bool(false)),
    }
    @post_init(destruction_request)

    /// A historically published pipeline absent from the compiled project.
    InactivePipeline { name, model_count, resource_count, last_published_at }

    DestructionRelationEvidence {
        database,
        name,
        kind(coerce = Coerce::Enum("DestructionRelationKind")),
        exists,
        total_bytes,
        active_parts,
        catalog_fingerprint,
        logical_names,
        pipeline_names,
        ownership,
        dependency_relation_names,
    }

    DestructionPlan {
        plan_id,
        operation,
        target,
        database,
        metadata_database,
        requested_pipeline_names,
        included_dependent_pipeline_names,
        affected_pipeline_names,
        affected_model_names,
        affected_source_names,
        relations,
        challenges,
        preserves_sources,
        preserves_replay_data,
        manifest_fingerprint,
        plan_fingerprint,
        created_at,
        expires_at,
        relation_drop_size_limit(default = Given::None),
        relation_drop_size_server_limit(default = Given::None),
        relation_drop_size_override(default = Given::None),
        relation_drop_size_policy_observed(default = Given::Bool(false)),
        include_orphans(default = Given::Bool(false)),
    }
    @impl {
        #[getter]
        fn estimated_bytes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
            let builtins: Bound<'py, PyModule> = py.import("builtins")?;
            let shares: Bound<'py, PyAny> = builtins
                .getattr("map")?
                .call1((wrap_pyfunction!(relation_bytes, py)?, self.relations.bind(py)))?;
            builtins.getattr("sum")?.call1((shares,))
        }
    }

    /// Terminal recorded outcome for one consumed frozen plan.
    DestructionExecutionResult {
        invocation_id,
        outcome,
        completed_statement_sequences,
        pending_statement_sequences,
        remaining_relation_names,
        error_message,
        residual_catalog_status(default = Given::Str("observed")),
        residual_catalog_error(default = Given::None),
    }

    /// Authenticated actor identity recorded with destructive execution evidence.
    DestructionActor { actor_id, actor_name }

    /// Immutable identity and connections shared by destructive recording phases.
    DestructionRecordingContext {
        plan,
        actor_id,
        actor_name,
        reviewed_at,
        confirmed_at,
        challenge_responses,
        connection,
        observation_connection,
        project_dir,
        started,
        statements,
        sink,
    }
}

shapes! {
    module = "sqltide.executor.readiness.models";

    /// Effective thresholds used to assess one staged deployment.
    DeploymentReadinessThresholds {
        maximum_lag_seconds(default = Given::Float(30.0)),
        minimum_staged_row_ratio(default = Given::Float(0.5)),
    }

    /// Input required to audit a staged backfill deployment.
    DeploymentAuditRequest {
        deployment_id,
        metadata_database,
        default_database,
        thresholds(default = Given::Factory(factory::<DeploymentReadinessThresholds>)),
    }

    /// A deployment candidate surfaced when audit resolution is ambiguous.
    AuditDeploymentCandidate {
        deployment_id,
        created_at(default = Given::None),
        deployment_status(default = Given::None),
        root_names(default = Given::EmptyTuple),
    }

    /// Persisted deployment metadata used by metadata-backed flows.
    LoadedAuditDeployment {
        deployment_id,
        created_at,
        status,
        replay_lineage_mode(coerce = REPLAY_LINEAGE_MODE),
        warning_codes,
        root_keys,
        prepared_object_mappings,
    }

    /// Partitioned catch-up metrics for offset-based replay.
    OffsetCatchupSummary {
        active_partition_count,
        staged_partition_count,
        partitions_compared,
        missing_staged_partition_count,
        missing_freshness_partition_count,
        lagging_partition_count,
        max_offset_gap,
        average_offset_gap,
        lag_boundary_column,
        max_lag_seconds,
        average_lag_seconds,
    }

    /// Scalar catch-up metrics for timestamp-based replay.
    ScalarCatchupSummary {
        active_min_value,
        active_max_value,
        staged_min_value,
        staged_max_value,
        lag_seconds,
    }

    /// Audit summary for one rebuilt root target.
    RootAuditResult {
        root_key,
        staged_physical_name,
        staged_exists,
        active_exists,
        active_row_count,
        staged_row_count,
        row_delta,
        row_ratio,
        assessment(coerce = Coerce::Enum("AuditAssessment")),
        replay_lineage_mode(coerce = REPLAY_LINEAGE_MODE),
        offset_catchup_summary,
        scalar_catchup_summary,
        state,
        replay_source_name,
        replay_source_row_count,
        warnings(default = Given::EmptyTuple),
    }

    /// Structured audit result for a staged backfill deployment.
    DeploymentAuditResult {
        deployment_id,
        deployment_status,
        assessment(coerce = Coerce::Enum("AuditAssessment")),
        replay_lineage_mode(coerce = REPLAY_LINEAGE_MODE),
        warning_codes,
        root_results,
        quality_check_results(default = Given::EmptyTuple),
        thresholds(default = Given::Factory(factory::<DeploymentReadinessThresholds>)),
    }
}

shapes! {
    module = "sqltide.executor.deployment.models";

    /// One deployment reconstructed from authoritative warehouse evidence.
    DeploymentSummary {
        deployment_id,
        state,
        created_at,
        persisted_status,
        root_names,
        physical_relation_names,
        missing_physical_relation_names,
        active_binding_names,
        latest_published_at,
    }

    /// Deterministically ordered deployment summaries for one target database.
    DeploymentInventory { database, deployments }

    /// One active/deployment comparison expression.
    DeploymentDiffRequest { database, metadata_database, comparison }

    /// One catalog column included in a deployment comparison.
    DeploymentDiffColumn { name, r#type, default_expression }

    /// Schema and count comparison for one logical relation.
    DeploymentDiffRelation {
        database,
        logical_name,
        status(coerce = Coerce::Enum("DeploymentDiffStatus")),
        from_physical_name,
        to_physical_name,
        from_columns,
        to_columns,
        from_row_count,
        to_row_count,
    }

    /// Resolved endpoints and per-relation deployment differences.
    DeploymentDiffResult { database, from_endpoint, to_endpoint, relations }
}

shapes! {
    module = "sqltide.executor.auditing.models";

    /// One executed SQL audit and its outcome.
    SqlAuditResult {
        file_path,
        referenced_model_names,
        severity,
        passed,
        failing_row_count,
        sample_column_names,
        sample_rows,
        description(default = Given::None),
        name(default = Given::None),
        error_message(default = Given::None),
        deferred_until(default = Given::None),
        missing_relation_names(default = Given::EmptyTuple),
    }
    @impl {
        /// Return whether execution was deferred for warmup or materialization.
        #[getter]
        fn deferred(&self, py: Python<'_>) -> PyResult<bool> {
            Ok(!self.deferred_until.is_none(py)
                || self.missing_relation_names.bind(py).is_truthy()?)
        }
    }

    /// Aggregate outcome for a batch of SQL audits.
    SqlAuditRunResult { audit_results }
    @impl {
        #[getter]
        fn error_failure_count(&self, py: Python<'_>) -> PyResult<usize> {
            failure_count(self.audit_results.bind(py), "ERROR")
        }

        #[getter]
        fn warning_failure_count(&self, py: Python<'_>) -> PyResult<usize> {
            failure_count(self.audit_results.bind(py), "WARNING")
        }
    }

    /// Eligibility of one audit relative to its newest referenced model anchor.
    AuditWarmupState {
        eligible,
        anchor,
        eligible_at,
        missing_model_names(default = Given::EmptyTuple),
    }
}

shapes! {
    module = "sqltide.executor.observability.models";

    /// Pre-execution work completed before the first durable run event.
    RunStartupTimings { compile_ms, observability_ms, planning_ms }
    @impl {
        #[getter]
        fn total_ms<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
            self.compile_ms
                .bind(py)
                .add(self.observability_ms.bind(py))?
                .add(self.planning_ms.bind(py))
        }
    }

    /// Command outcome fields captured at one finite CLI boundary.
    TerminalInvocation {
        project_dir,
        target_identity,
        command,
        mode,
        outcome,
        exit_code,
        materialized_outcome,
        deployment_id,
        workflow_id,
        selected_node_count,
        error_message,
        summary,
    }

    /// Trigger, logical slot, and policy snapshot for one quality attempt.
    QualityResultContext {
        trigger(coerce = Coerce::Enum("QualityResultTrigger")),
        scheduled_for(default = Given::None),
        cadence_seconds(default = Given::None),
        warmup_seconds(default = Given::Int(0)),
    }
}

shapes! {
    module = "sqltide.executor.janitor.models";

    JanitorPreviewCandidate {
        deployment_id,
        created_at,
        status,
        logical_view_names,
        physical_object_names,
        deletable,
        reason,
    }

    JanitorPreviewResult {
        database,
        retention_days,
        minimum_rollback_deployments,
        candidates,
    }

    JanitorApplyResult {
        database,
        retention_days,
        minimum_rollback_deployments,
        deleted_deployment_ids,
        deleted_object_names,
    }

    JanitorRequest {
        database,
        metadata_database,
        retention_days,
        apply,
        minimum_rollback_deployments(default = Given::Int(2)),
    }
    @post_init(janitor_request)
}

shapes! {
    module = "sqltide.executor.reconcile.models";

    ReconcileRejectedTarget { target_key, target_name, reasons }

    ReconcilePreview { database, reconcile_id, eligible_records, rejected_targets }

    ReconcileResult { database, reconcile_id, reconciled_records, rejected_targets }
}

shapes! {
    module = "sqltide.executor.testing.models";

    /// One executed target comparison inside a SQL-native test scenario.
    SqlTestTargetExecutionResult {
        target_model_name,
        passed,
        column_names,
        missing_rows,
        unexpected_rows,
    }

    /// One executed SQL test scenario and its aggregate diff outcome.
    SqlTestExecutionResult {
        file_path,
        passed,
        target_results,
        executed_sql,
        warnings(default = Given::EmptyTuple),
        name(default = Given::None),
        test_index(default = Given::Int(1)),
        error_message(default = Given::None),
    }
}

shapes! {
    module = "sqltide.executor.doctor.models";

    /// Input required to inspect active-view health.
    DoctorRequest { default_database }

    /// Diagnosis for one logical managed table.
    ActiveViewStatus {
        table_name,
        state_kind,
        active_deployment_id,
        candidate_deployment_ids,
    }

    /// Read-only diagnosis of active-view health.
    DoctorResult { active_views }
}

/// A native enum: its class, public module, docstring and members.
type EnumSpec = (
    &'static str,
    &'static str,
    Option<&'static str>,
    &'static [(&'static str, &'static str)],
);

const ENUMS: [EnumSpec; 11] = [
    (
        "DestructionOperation",
        "sqltide.executor.destruction.types",
        None,
        &[
            ("DESTROY_PIPELINES", "destroy_pipelines"),
            ("RESET_TARGET", "reset_target"),
        ],
    ),
    (
        "DestructionRelationKind",
        "sqltide.executor.destruction.types",
        None,
        &[
            ("VIEW", "view"),
            ("MATERIALIZED_VIEW", "materialized_view"),
            ("TABLE", "table"),
            ("MANAGED_SOURCE", "managed_source"),
        ],
    ),
    (
        "DestructionOwnership",
        "sqltide.executor.destruction.types",
        None,
        &[
            ("CURRENT_MANIFEST", "current_manifest"),
            ("VIRTUAL_PHYSICAL_MAPPING", "virtual_physical_mapping"),
            ("PUBLISHED_STABLE_BINDING", "published_stable_binding"),
            ("OWNERSHIP_LEDGER", "ownership_ledger"),
            ("HISTORICAL_MANIFEST", "historical_manifest"),
        ],
    ),
    (
        "DestructionPlanningErrorCode",
        "sqltide.executor.destruction.types",
        Some("Why the native planner rejected a destruction request."),
        &[
            ("SELECTION", "selection"),
            ("UNKNOWN_PIPELINES", "unknown_pipelines"),
            ("DEPENDENCY", "dependency"),
            ("RESOURCE", "resource"),
            ("UNSUPPORTED_HISTORICAL_KIND", "unsupported_historical_kind"),
            ("INVALID_OBJECT_TYPE", "invalid_object_type"),
        ],
    ),
    (
        "AuditAssessment",
        "sqltide.executor.readiness.types",
        None,
        &[
            ("READY", "ready"),
            ("NOT_READY", "not_ready"),
            ("CAUTION", "caution"),
        ],
    ),
    (
        "DeploymentLifecycleState",
        "sqltide.executor.deployment.types",
        Some("Authoritative state derived from metadata and live catalog evidence."),
        &[
            ("ACTIVE", "active"),
            ("STAGED", "staged"),
            ("SUPERSEDED", "superseded"),
            ("INCOMPLETE", "incomplete"),
            ("METADATA_MISSING", "metadata_missing"),
            ("PHYSICAL_MISSING", "physical_missing"),
        ],
    ),
    (
        "DeploymentDiffStatus",
        "sqltide.executor.deployment.types",
        Some("Summary classification for one logical relation comparison."),
        &[
            ("ADDED", "added"),
            ("REMOVED", "removed"),
            ("CHANGED", "changed"),
            ("UNCHANGED", "unchanged"),
            ("PHYSICAL_MISSING", "physical_missing"),
        ],
    ),
    (
        "AuditSeverity",
        "sqltide.executor.auditing.types",
        Some("How a failing audit should be reported."),
        &[("ERROR", "error"), ("WARNING", "warning")],
    ),
    (
        "QualityResultStatus",
        "sqltide.executor.auditing.types",
        Some("Persisted status of one audit or SQL-test attempt."),
        &[
            ("PASSED", "passed"),
            ("WARNING", "warning"),
            ("FAILED", "failed"),
            ("ERROR", "error"),
            ("DEFERRED", "deferred"),
        ],
    ),
    (
        "QualityResultTrigger",
        "sqltide.executor.observability.types",
        Some("The execution path that produced one quality result."),
        &[
            ("BUILD", "build"),
            ("MANUAL", "manual"),
            ("SCHEDULED", "scheduled"),
            ("DEPLOYMENT", "deployment"),
        ],
    ),
    (
        "MaterializationOutcome",
        "sqltide.executor.observability.types",
        Some("Whether a direct mutation workflow safely completed."),
        &[("APPLIED", "applied"), ("FAILED", "failed")],
    ),
];

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    for (name, public_module, doc, members) in ENUMS {
        str_enum(module, name, public_module, name, doc, members)?;
    }
    Ok(())
}

#[pyfunction]
#[pyo3(name = "__init__")]
fn destruction_dependency_init<'py>(
    instance: &Bound<'py, PyAny>,
    dependent_pipeline_names: Bound<'py, PyAny>,
) -> PyResult<()> {
    let message: String = format!(
        "Unselected dependent or shared-source pipelines must be explicitly included: {}",
        dependent_pipeline_names.repr()?
    );
    instance.setattr("dependent_pipeline_names", dependent_pipeline_names)?;
    base_init(instance, [message.into_pyobject(instance.py())?.into_any()])
}

#[pyfunction]
#[pyo3(name = "__init__")]
fn destruction_external_dependency_init<'py>(
    instance: &Bound<'py, PyAny>,
    relation_names: Bound<'py, PyAny>,
) -> PyResult<()> {
    let message: String = format!(
        "Unmanaged warehouse dependants block destruction: {}",
        relation_names.repr()?
    );
    instance.setattr("relation_names", relation_names)?;
    base_init(instance, [message.into_pyobject(instance.py())?.into_any()])
}

/// The destruction errors under `DestructionError` without their own constructor: name, base
/// and docstring.
const DESTRUCTION_ERRORS: [(&str, &str, &str); 10] = [
    (
        "DestructionConsistencyError",
        "DestructionError",
        "Internal destruction evidence became inconsistent.",
    ),
    (
        "DestructionPlanNotFoundError",
        "DestructionError",
        "The requested frozen plan does not exist or was already consumed.",
    ),
    (
        "DestructionPlanExpiredError",
        "DestructionError",
        "The requested frozen plan has expired.",
    ),
    (
        "DestructionPlanNotReviewedError",
        "DestructionError",
        "The frozen plan has not passed the explicit review gate.",
    ),
    (
        "DestructionPlanCorruptError",
        "DestructionError",
        "A durable frozen-plan record is incompatible or corrupt.",
    ),
    (
        "DestructionChallengeError",
        "DestructionError",
        "Typed challenge responses do not exactly match the frozen plan.",
    ),
    (
        "DestructionDriftError",
        "DestructionError",
        "A freshly planned operation differs from its frozen plan.",
    ),
    (
        "DestructionRecordingError",
        "DestructionError",
        "Required destructive-operation evidence could not be persisted.",
    ),
    (
        "DestructionRecoveryError",
        "DestructionError",
        "A failed run does not contain valid recoverable destruction intent.",
    ),
    (
        "DestructionRecoveryNotFoundError",
        "DestructionRecoveryError",
        "A run is not a recoverable operation in the active project.",
    ),
];

/// The other executor errors: public module, name, docstring and whether `RuntimeError` rather
/// than `ValueError` is the base.
const EXECUTOR_ERRORS: [(&str, &str, &str, bool); 7] = [
    (
        "sqltide.executor.readiness.exceptions",
        "DeploymentAuditExecutionError",
        "Raised when audit backfill input or state is invalid.",
        false,
    ),
    (
        "sqltide.executor.deployment.exceptions",
        "DeploymentNotFoundError",
        "Raised when an explicit deployment identifier is not present.",
        false,
    ),
    (
        "sqltide.executor.deployment.exceptions",
        "DeploymentDiffError",
        "Raised when a deployment comparison cannot be resolved safely.",
        false,
    ),
    (
        "sqltide.executor.auditing.exceptions",
        "AuditExecutionError",
        "Raised when auditing input or state is invalid.",
        false,
    ),
    (
        "sqltide.executor.janitor.exceptions",
        "JanitorInputError",
        "Raised when janitor retention input is invalid.",
        false,
    ),
    (
        "sqltide.executor.reconcile.exceptions",
        "ReconcileError",
        "Raised when verified reconcile state cannot be persisted safely.",
        true,
    ),
    (
        "sqltide.executor.testing.exceptions",
        "SqlTestExecutionError",
        "Raised when testing input or state is invalid.",
        false,
    ),
];

fn exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    let value_error: Bound<'_, PyAny> = py.get_type::<PyValueError>().into_any();
    let runtime_error: Bound<'_, PyAny> = py.get_type::<PyRuntimeError>().into_any();
    let base: Bound<'_, PyAny> = exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionError",
        "Base error for the destruction domain.",
        std::slice::from_ref(&runtime_error),
        None,
        None,
    )?;
    let selection: Bound<'_, PyAny> = exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionSelectionError",
        "The requested pipeline selection is invalid.",
        std::slice::from_ref(&base),
        None,
        None,
    )?;
    exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionValidationError",
        "A destruction value violates a domain invariant.",
        &[base.clone(), value_error.clone()],
        None,
        None,
    )?;
    exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionResourceError",
        "A manifest resource cannot participate in destruction.",
        &[base.clone(), py.get_type::<PyTypeError>().into_any()],
        None,
        None,
    )?;
    exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionDependencyError",
        "Unselected downstream pipelines make destruction unsafe.",
        std::slice::from_ref(&selection),
        Some(wrap_pyfunction!(destruction_dependency_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        DESTRUCTION_EXCEPTIONS,
        "DestructionExternalDependencyError",
        "Warehouse relations outside the owned scope depend on planned relations.",
        std::slice::from_ref(&selection),
        Some(wrap_pyfunction!(destruction_external_dependency_init, module)?.into_any()),
        None,
    )?;
    for (name, parent, doc) in DESTRUCTION_ERRORS {
        let parent: Bound<'_, PyAny> = module.getattr(parent)?;
        exception(
            module,
            DESTRUCTION_EXCEPTIONS,
            name,
            doc,
            &[parent],
            None,
            None,
        )?;
    }
    for (public_module, name, doc, runtime) in EXECUTOR_ERRORS {
        let parent: &Bound<'_, PyAny> = if runtime {
            &runtime_error
        } else {
            &value_error
        };
        exception(
            module,
            public_module,
            name,
            doc,
            std::slice::from_ref(parent),
            None,
            None,
        )?;
    }
    Ok(())
}

const TEXT: [(&str, &str); 5] = [
    ("ACTIVE_DEPLOYMENT_DIFF_ENDPOINT", "active"),
    ("RUN_DISPLAY_COMMAND_ENV_VAR", "SQLTIDE_RUN_DISPLAY_COMMAND"),
    ("RUN_INVOCATION_ID_ENV_VAR", "SQLTIDE_RUN_INVOCATION_ID"),
    ("PROJECT_REVISION_ENV_VAR", "SQLTIDE_PROJECT_REVISION"),
    ("RECONCILE_DEPLOYMENT_ID_PREFIX", "reconcile_"),
];

fn constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    add(
        module,
        "DEFAULT_DESTRUCTION_PLAN_TTL",
        py.import("datetime")?
            .getattr("timedelta")?
            .call((), Some(&[("minutes", 15)].into_py_dict(py)?))?,
    )?;
    add(module, "DESTRUCTION_PLAN_PAYLOAD_VERSION", 1)?;
    add(
        module,
        "PRODUCTION_TARGET_NAMES",
        PyFrozenSet::new(py, ["prod", "production"])?,
    )?;
    add(module, "ACCEPTABLE_LAG_SECONDS", 30.0)?;
    add(module, "MINIMUM_STAGED_ROW_RATIO", 0.5)?;
    add(module, "EXPLICIT_DEPLOYMENT_DIFF_ENDPOINT_COUNT", 2)?;
    add(module, "AUDIT_SAMPLE_LIMIT", 5)?;
    add(module, "MAX_OBSERVATION_JSON_BYTES", 16_384)?;
    add(module, "MAX_OBSERVATION_ERROR_LENGTH", 1_048_576)?;
    add(module, "HEARTBEAT_INTERVAL_SECONDS", 10.0)?;
    for (name, value) in TEXT {
        add(module, name, value)?;
    }
    Ok(())
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    exceptions(module)?;
    constants(module)?;
    add_class::<DestructionRequest>(module)?;
    add_class::<InactivePipeline>(module)?;
    add_class::<DestructionRelationEvidence>(module)?;
    add_class::<DestructionPlan>(module)?;
    add_class::<DestructionExecutionResult>(module)?;
    add_class::<DestructionActor>(module)?;
    add_class::<DestructionRecordingContext>(module)?;
    add_class::<DeploymentReadinessThresholds>(module)?;
    add_class::<DeploymentAuditRequest>(module)?;
    add_class::<AuditDeploymentCandidate>(module)?;
    add_class::<LoadedAuditDeployment>(module)?;
    add_class::<OffsetCatchupSummary>(module)?;
    add_class::<ScalarCatchupSummary>(module)?;
    add_class::<RootAuditResult>(module)?;
    add_class::<DeploymentAuditResult>(module)?;
    add_class::<DeploymentSummary>(module)?;
    add_class::<DeploymentInventory>(module)?;
    add_class::<DeploymentDiffRequest>(module)?;
    add_class::<DeploymentDiffColumn>(module)?;
    add_class::<DeploymentDiffRelation>(module)?;
    add_class::<DeploymentDiffResult>(module)?;
    add_class::<SqlAuditResult>(module)?;
    add_class::<SqlAuditRunResult>(module)?;
    add_class::<AuditWarmupState>(module)?;
    add_class::<RunStartupTimings>(module)?;
    add_class::<TerminalInvocation>(module)?;
    add_class::<QualityResultContext>(module)?;
    add_class::<JanitorPreviewCandidate>(module)?;
    add_class::<JanitorPreviewResult>(module)?;
    add_class::<JanitorApplyResult>(module)?;
    add_class::<JanitorRequest>(module)?;
    add_class::<ReconcileRejectedTarget>(module)?;
    add_class::<ReconcilePreview>(module)?;
    add_class::<ReconcileResult>(module)?;
    add_class::<SqlTestTargetExecutionResult>(module)?;
    add_class::<SqlTestExecutionResult>(module)?;
    add_class::<DoctorRequest>(module)?;
    add_class::<ActiveViewStatus>(module)?;
    add_class::<DoctorResult>(module)
}
