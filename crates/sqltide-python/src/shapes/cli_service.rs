//! Shapes of the CLI, accounts and authorization, warehouse workflows, sensors, the dev server
//! and diagnostics, plus their value enums, exceptions and constants.

use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{IntoPyDict, PyDict, PyFrozenSet, PyTuple};

use super::{
    Coerce, Given, add, add_class, base_init, exception, member_set, native_enum, raised, shapes,
    str_enum,
};

const WORKFLOW_PHASES: [&str; 9] = [
    "PREFLIGHT",
    "PREPARATION",
    "TEARDOWN",
    "REALIZATION",
    "STABILIZATION",
    "BOUNDARY",
    "REPLAY",
    "AUDIT",
    "FINALIZATION",
];

/// `importlib.metadata.version("sqltide")`, read on first use.
pub(super) fn tool_version(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("importlib.metadata")?
        .getattr("version")?
        .call1(("sqltide",))
}

/// The workflow phases in execution order, built on first use because `WorkflowPhase` is still
/// defined in Python.
pub(super) fn workflow_phase_order(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let phase: Bound<'_, PyAny> = py
        .import("sqltide.executor.workflow.types")?
        .getattr("WorkflowPhase")?;
    let mut members: Vec<Bound<'_, PyAny>> = Vec::with_capacity(WORKFLOW_PHASES.len());
    for name in WORKFLOW_PHASES {
        members.push(phase.getattr(name)?);
    }
    Ok(PyTuple::new(py, members)?.into_any())
}

/// `WorkflowValidationError(message)`, from the Python workflow exceptions.
fn invalid_workflow(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import("sqltide.executor.workflow.exceptions")?
            .getattr("WorkflowValidationError")?
            .call1((message,))?,
    ))
}

/// `WarehouseStatement`: a word-character step ID and SQL ending in exactly one semicolon.
fn warehouse_statement(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [_, step_id, _, _, sql, ..] = values else {
        return Err(PyValueError::new_err(
            "warehouse statement bound too few values",
        ));
    };
    let py: Python<'_> = step_id.py();
    if !step_id.is_truthy()?
        || !step_id
            .call_method1("replace", ("_", ""))?
            .call_method0("isalnum")?
            .is_truthy()?
    {
        return Err(invalid_workflow(
            py,
            "Workflow step IDs may contain only letters, numbers, and underscores".to_owned(),
        )?);
    }
    if !sql.call_method1("endswith", (";",))?.is_truthy()?
        || sql.call_method1("endswith", (";;",))?.is_truthy()?
    {
        return Err(invalid_workflow(
            py,
            format!(
                "Workflow statement {} must end with exactly one semicolon",
                step_id.repr()?
            ),
        )?);
    }
    Ok(())
}

/// `BuildWorkflow`: statement sequences run continuously from one and phases never go back.
fn build_workflow(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [_, _, statements, ..] = values else {
        return Err(PyValueError::new_err("build workflow bound too few values"));
    };
    let py: Python<'_> = statements.py();
    let order: Bound<'_, PyAny> = py
        .import("sqltide._native")?
        .getattr("WORKFLOW_PHASE_ORDER")?;
    let mut previous_phase_index: usize = 0;
    for (expected_sequence, statement) in (1_i64..).zip(statements.try_iter()?) {
        let statement: Bound<'_, PyAny> = statement?;
        if statement.getattr("sequence")?.ne(expected_sequence)? {
            return Err(invalid_workflow(
                py,
                "Workflow statement sequences must be continuous from one".to_owned(),
            )?);
        }
        let phase_index: usize = order
            .call_method1("index", (statement.getattr("phase")?,))?
            .extract()?;
        if phase_index < previous_phase_index {
            return Err(invalid_workflow(
                py,
                "Workflow phases must be monotonic".to_owned(),
            )?);
        }
        previous_phase_index = phase_index;
    }
    Ok(())
}

/// `AuthSettings`: a positive session TTL, and trusted-proxy auto-provisioning only as viewer.
fn auth_settings(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    let [
        mode,
        _,
        _,
        _,
        _,
        unknown_user_policy,
        default_role,
        _,
        session_ttl_seconds,
        ..,
    ] = values
    else {
        return Err(PyValueError::new_err("auth settings bound too few values"));
    };
    let py: Python<'_> = mode.py();
    if session_ttl_seconds.le(0)? {
        return Err(raised(
            py,
            "AuthConfigurationError",
            "Session TTL must be positive".to_owned(),
        )?);
    }
    if mode.eq(native_enum(py, "AuthenticationMode")?.getattr("TRUSTED_PROXY")?)?
        && unknown_user_policy
            .eq(native_enum(py, "UnknownUserPolicy")?.getattr("AUTO_PROVISION")?)?
        && default_role.ne("viewer")?
    {
        return Err(raised(
            py,
            "AuthConfigurationError",
            "Trusted-proxy auto-provisioning default role must be 'viewer'".to_owned(),
        )?);
    }
    Ok(())
}

shapes! {
    module = "sqltide.diagnostics.models";

    /// One one-based source span.
    SourceLocation {
        path,
        line,
        column,
        end_line(default = Given::None),
        end_column(default = Given::None),
    }

    /// One labeled source span related to a primary diagnostic.
    RelatedDiagnosticLocation { label, location, message(default = Given::None) }

    /// One structured compiler or runtime diagnostic.
    CompilerDiagnostic {
        phase(coerce = Coerce::Enum("DiagnosticPhase")),
        severity(coerce = Coerce::Enum("DiagnosticSeverity")),
        code,
        message,
        resource_name(default = Given::None),
        location(default = Given::None),
        related_locations(default = Given::EmptyTuple),
        help(default = Given::None),
    }
}

shapes! {
    module = "sqltide.auth.models";

    /// Authenticated request identity, independent of authorization policy.
    Principal { user_id, username, display_name, email, authentication_source }

    /// Server-runtime authentication and control-store settings.
    AuthSettings @no_repr {
        mode,
        control_store_url,
        username_header(default = Given::Str("X-SQLTide-User")),
        display_name_header(default = Given::None),
        email_header(default = Given::None),
        unknown_user_policy(default = Given::Member("UnknownUserPolicy", "AUTO_PROVISION")),
        default_role(default = Given::Str("viewer")),
        session_cookie_name(default = Given::Str("sqltide_session")),
        session_ttl_seconds(default = Given::Int(43_200)),
        session_cookie_secure(default = Given::Bool(true)),
        proxy_logout_url(default = Given::None),
    }
    @post_init(auth_settings)
    @impl {
        fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
            let driver: Bound<'_, PyAny> = self
                .control_store_url
                .bind(py)
                .call_method1("partition", (":",))?
                .get_item(0)?;
            Ok(format!(
                "AuthSettings(mode={}, control_store_url='<redacted:{}>', username_header={}, \
                 unknown_user_policy={}, default_role={})",
                self.mode.bind(py).repr()?,
                driver.str()?,
                self.username_header.bind(py).repr()?,
                self.unknown_user_policy.bind(py).repr()?,
                self.default_role.bind(py).repr()?,
            ))
        }
    }

    /// Persisted SQLTide account without credential material.
    UserAccount {
        user_id,
        username,
        display_name,
        email,
        is_active,
        created_at,
        updated_at,
        roles(default = Given::EmptyTuple),
        authentication_sources(default = Given::EmptyTuple),
    }

    /// One newly issued browser session; only the hash is persisted.
    SessionCredentials { token, csrf_token, expires_at }

    /// Active browser session and its authenticated principal.
    ResolvedSession { principal, roles, csrf_token, expires_at }

    /// Principal plus request-local account/session state.
    AuthenticatedRequest { principal, roles, csrf_token(default = Given::None) }

    /// Non-secret account administration audit event.
    AccountAuditRecord { operation, actor_user_id, affected_user_id, occurred_at, details }

    /// Audited user membership in one project-authored operational role.
    ProjectRoleAssignment {
        assignment_id,
        user_id,
        project_name,
        role_name,
        target_name,
        assigned_by,
        assigned_at,
        revoked_by(default = Given::None),
        revoked_at(default = Given::None),
    }
}

shapes! {
    module = "sqltide.authorization.models";

    /// Authoritative identity, policy, and resolved operation scope.
    AuthorizationRequest {
        authenticated,
        permission,
        project_name,
        target_name,
        grant_scope,
        affected_pipelines,
        policy,
    }

    /// Explainable allow or deny result from centralized evaluation.
    AuthorizationDecision {
        allowed,
        reason,
        permission,
        project_name,
        target_name,
        affected_pipelines,
        matched_roles(default = Given::EmptyTuple),
        missing_pipelines(default = Given::EmptyTuple),
    }

    /// Identity and policy inputs for one effective-capability summary.
    CapabilityRequest { authenticated, project_name, target_name, pipeline_names, policy }

    /// UI-facing effective operation permissions for one user.
    EffectiveCapabilities {
        system_admin,
        project_name,
        target_name,
        permissions,
        pipeline_permissions,
        stale_roles(default = Given::EmptyTuple),
    }
}

shapes! {
    module = "sqltide.executor.workflow.models";

    WarehouseStatement {
        sequence,
        step_id,
        phase,
        intent,
        sql,
        continue_on_error(default = Given::Bool(false)),
        display_name(default = Given::None),
        replay_offset_progress(default = Given::None),
    }
    @post_init(warehouse_statement)

    BuildWorkflow {
        mode,
        plan_json,
        statements,
        recovery_statements(default = Given::EmptyTuple),
    }
    @post_init(build_workflow)

    PublishedBuildWorkflow { workflow, artifact_root, workflow_sha256 }

    WorkflowStatementResult {
        step_id,
        query_result,
        mutation_result,
        error_message(default = Given::None),
    }

    WorkflowExecutionResult { statement_results }
}

shapes! {
    module = "sqltide.cli.build.models";

    /// Every operator-supplied option for one `sqt build` invocation.
    BuildCommandOptions {
        pipelines_root,
        database,
        metadata_database,
        selectors,
        json_output,
        verbose,
        auto_approve,
        deployment_id(default = Given::None),
        full_refresh(default = Given::Bool(false)),
        start_time(default = Given::None),
        events_output(default = Given::Bool(false)),
        confirmations(default = Given::EmptyTuple),
        changed(default = Given::Bool(false)),
        include_missing_upstream(default = Given::Bool(false)),
    }

    /// One protected pipeline touched by the resolved build closure.
    BuildProtectionRequirement { pipeline_name, warning, confirmation }

    /// Connected planning options shared by `sqt plan` and `sqt build`.
    WorkflowPreparationOptions {
        database,
        metadata_database,
        selectors,
        deployment_id,
        full_refresh,
        start_time,
        verbose,
        changed(default = Given::Bool(false)),
        include_missing_upstream(default = Given::Bool(false)),
    }

    /// The plan and resolved databases a build renders before it writes anything.
    DirectBuildPreviewContext {
        analysis,
        plan,
        warehouse_snapshot,
        database,
        metadata_database,
        adapter_name,
        effective_start_time(default = Given::None),
        row_error_policy_changes(default = Given::EmptyTuple),
    }

    /// The confirmed virtual deployment plan and its fixed execution identity.
    VirtualBuildPreviewContext {
        database,
        metadata_database,
        desired_state,
        plan,
        replay_lineage_mode,
        deployment_id,
        created_at,
        root_reports,
        existing_relation_names,
        target_catalog,
        metadata_catalog,
        full_refresh_keys(default = Given::EmptyFrozenSet),
        start_time_keys(default = Given::EmptyFrozenSet),
        start_time(default = Given::None),
        execution_logical_model_keys(default = Given::EmptyFrozenSet),
        run_execution_scope(default = Given::EmptyTuple),
        run_context_scope(default = Given::EmptyTuple),
        row_error_policy_changes(default = Given::EmptyTuple),
    }

    /// One fully assembled direct workflow and its result-decoding request.
    DirectWorkflowPreparation {
        preview,
        request,
        workflow,
        plan_text,
        protection_requirements(default = Given::EmptyTuple),
    }

    /// One fully assembled virtual workflow and its result-decoding request.
    VirtualWorkflowPreparation {
        preview,
        request,
        workflow,
        plan_text,
        protection_requirements(default = Given::EmptyTuple),
    }

    /// Virtual-first and direct-second workflows prepared for one build.
    MixedWorkflowPreparation {
        r#virtual,
        direct,
        plan_text,
        plan_json,
        protection_requirements(default = Given::EmptyTuple),
    }
}

shapes! {
    module = "sqltide.cli.destruction.models";

    DestructionCommandOptions @no_repr {
        operation,
        pipelines_root,
        project_dir,
        selected_target,
        database,
        selectors,
        control_store_url,
        include_orphans(default = Given::Bool(false)),
        cli_variables(default = Given::EmptyTuple),
        environment(default = Given::None),
    }
}

shapes! {
    module = "sqltide.cli.dev.models";

    /// Parsed `sqt dev` invocation options.
    DevCommandOptions @no_repr {
        pipelines_root,
        database,
        host,
        port,
        selected_target(default = Given::None),
        cli_variables(default = Given::EmptyTuple),
        environment(default = Given::None),
        connection_host(default = Given::None),
        connection_port(default = Given::None),
        connection_username(default = Given::None),
        connection_password(default = Given::None),
        auth_settings(default = Given::None),
    }
}

shapes! {
    module = "sqltide.cli.entry.models";

    CliEntrypointHandlers {
        run_discover,
        run_compile,
        run_test,
        run_audit,
        run_plan,
        run_build,
        run_destruction,
        run_deployment_diff,
        run_deployment_list,
        run_deployment_show,
        run_deployment_audit,
        run_deployment_promote,
        run_deployment_rollback,
        run_reconcile,
        run_janitor,
        run_doctor,
        run_repair_active_view,
        run_dev,
    }
}

shapes! {
    module = "sqltide.cli.plan.models";

    /// Every operator-supplied option for one `sqt plan` invocation.
    PlanCommandOptions {
        pipelines_root,
        database,
        selectors,
        deployment_id,
        full_refresh,
        start_time,
        json_output,
        verbose,
        changed(default = Given::Bool(false)),
        include_missing_upstream(default = Given::Bool(false)),
    }
}

shapes! {
    module = "sqltide.cli.selection.models";

    SelectionResolution {
        desired_state,
        selected_logical_model_keys,
        selected_model_keys,
        replay_lineage_mode,
        execution_logical_model_keys(default = Given::EmptyFrozenSet),
    }
}

/// A native enum: its class, public module, docstring and members.
type EnumSpec = (
    &'static str,
    &'static str,
    Option<&'static str>,
    &'static [(&'static str, &'static str)],
);

const DEV_SERVER_TYPES: &str = "sqltide.dev_server.types";

const ENUMS: [EnumSpec; 14] = [
    (
        "AdminCommand",
        "sqltide.cli.admin.types",
        Some("Supported operator account-administration commands."),
        &[
            ("MIGRATE", "migrate"),
            ("CREATE_USER", "create-user"),
            ("GRANT_ROLE", "grant-role"),
            ("REVOKE_ROLE", "revoke-role"),
            ("ENABLE_USER", "enable-user"),
            ("DISABLE_USER", "disable-user"),
            ("RESET_PASSWORD", "reset-password"),
        ],
    ),
    (
        "CliCommand",
        "sqltide.cli.entry.types",
        Some("One top-level `sqt` subcommand as argparse reports it."),
        &[
            ("ADMIN", "admin"),
            ("PLAYGROUND", "playground"),
            ("DISCOVER", "discover"),
            ("DEV", "dev"),
            ("COMPILE", "compile"),
            ("LINT", "lint"),
            ("FORMAT", "format"),
            ("TEST", "test"),
            ("PLAN", "plan"),
            ("BUILD", "build"),
            ("DESTROY", "destroy"),
            ("RESET_TARGET", "reset-target"),
            ("DEPLOYMENT", "deployment"),
            ("AUDIT", "audit"),
            ("RECONCILE", "reconcile"),
            ("RENAME_PROJECT", "rename-project"),
            ("JANITOR", "janitor"),
            ("DOCTOR", "doctor"),
            ("REPAIR", "repair"),
        ],
    ),
    (
        "CliSubcommand",
        "sqltide.cli.entry.types",
        Some("One nested `sqt <command> <subcommand>` selector."),
        &[
            ("ACTIVE_VIEW", "active-view"),
            ("DIFF", "diff"),
            ("LIST", "list"),
            ("SHOW", "show"),
            ("AUDIT", "audit"),
            ("PROMOTE", "promote"),
            ("ROLLBACK", "rollback"),
        ],
    ),
    (
        "AuthenticationMode",
        "sqltide.auth.types",
        Some("Configured server authentication mechanism."),
        &[
            ("DISABLED", "disabled"),
            ("PASSWORD", "password"),
            ("TRUSTED_PROXY", "trusted_proxy"),
        ],
    ),
    (
        "AuthenticationSource",
        "sqltide.auth.types",
        Some("Mechanism that authenticated one request."),
        &[
            ("LOCAL", "local"),
            ("PASSWORD", "password"),
            ("TRUSTED_PROXY", "trusted_proxy"),
        ],
    ),
    (
        "UnknownUserPolicy",
        "sqltide.auth.types",
        Some("Trusted-proxy behavior for an identity absent from the control store."),
        &[("AUTO_PROVISION", "auto_provision"), ("DENY", "deny")],
    ),
    (
        "AuthorizationReason",
        "sqltide.authorization.types",
        Some("Stable explanation category for one operational decision."),
        &[
            ("SYSTEM_ADMIN", "system_admin"),
            ("GRANTED", "granted"),
            ("POLICY_ABSENT", "policy_absent"),
            ("NO_MATCHING_ASSIGNMENT", "no_matching_assignment"),
            ("STALE_ASSIGNMENT", "stale_assignment"),
            ("EMPTY_PIPELINE_SCOPE", "empty_pipeline_scope"),
            ("MISSING_PERMISSION", "missing_permission"),
            ("MISSING_PIPELINES", "missing_pipelines"),
        ],
    ),
    (
        "ActivityTone",
        DEV_SERVER_TYPES,
        Some("How an activity line should read: routine, good news, bad news, or caution."),
        &[
            ("NEUTRAL", "neutral"),
            ("GOOD", "good"),
            ("BAD", "bad"),
            ("CAUTION", "caution"),
        ],
    ),
    (
        "RunPresentationStatus",
        DEV_SERVER_TYPES,
        Some("Terminal facts and reversible liveness states shown for a run."),
        &[
            ("SUCCEEDED", "succeeded"),
            ("FAILED", "failed"),
            ("CANCELLED", "cancelled"),
            ("CANCELLING", "cancelling"),
            ("CANCELLATION_FAILED", "cancellation_failed"),
            ("RUNNING", "running"),
            ("UNRESPONSIVE", "unresponsive"),
            ("PRESUMED_FAILED", "presumed_failed"),
        ],
    ),
    (
        "CompileStateKind",
        DEV_SERVER_TYPES,
        Some("Whether the held project compile is servable or failing."),
        &[("OK", "ok"), ("FAILING", "failing")],
    ),
    (
        "ReplayAnchorReason",
        DEV_SERVER_TYPES,
        Some("Why a model is or is not a replay anchor."),
        &[
            ("ELIGIBLE", "eligible"),
            ("AGGREGATE", "aggregate"),
            ("MUTABLE_REF", "mutable_ref"),
            ("NEVER", "never"),
            ("LINEAGE_LOSS", "lineage_loss"),
            ("VIEW", "view"),
        ],
    ),
    (
        "Freshness",
        DEV_SERVER_TYPES,
        Some("Derived source or model freshness against its authored policy."),
        &[
            ("FRESH", "fresh"),
            ("LAGGING", "lagging"),
            ("STALLED", "stalled"),
        ],
    ),
    (
        "SensorSchedulerState",
        DEV_SERVER_TYPES,
        Some("Current lifecycle disposition of the hosted sensor dispatcher."),
        &[
            ("DISABLED", "disabled"),
            ("IDLE", "idle"),
            ("RUNNING", "running"),
            ("STANDBY", "standby"),
            ("BACKING_OFF", "backing_off"),
        ],
    ),
    (
        "AuditScheduleState",
        DEV_SERVER_TYPES,
        Some("Current scheduler disposition of one compiled audit or target."),
        &[
            ("DISABLED", "disabled"),
            ("IDLE", "idle"),
            ("DUE", "due"),
            ("SCHEDULED", "scheduled"),
            ("WARMING_UP", "warming_up"),
            ("NOT_MATERIALIZED", "not_materialized"),
            ("RUNNING", "running"),
            ("BLOCKED", "blocked"),
            ("BACKING_OFF", "backing_off"),
        ],
    ),
];

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    for (name, public_module, doc, members) in ENUMS {
        str_enum(module, name, public_module, name, doc, members)?;
    }
    Ok(())
}

#[pyfunction]
#[pyo3(name = "__init__", signature = (instance, message, *, hint = None))]
fn cli_user_init<'py>(
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    hint: Option<Bound<'py, PyAny>>,
) -> PyResult<()> {
    base_init(instance, [message])?;
    instance.setattr("hint", hint)
}

#[pyfunction]
#[pyo3(
    name = "__init__",
    signature = (
        instance,
        message,
        *,
        location = None,
        related_locations = Python::attach(|py| PyTuple::empty(py).into_any().unbind()),
    )
)]
fn sensor_init<'py>(
    py: Python<'py>,
    instance: &Bound<'py, PyAny>,
    message: Bound<'py, PyAny>,
    location: Option<Bound<'py, PyAny>>,
    related_locations: Py<PyAny>,
) -> PyResult<()> {
    base_init(instance, [message.clone()])?;
    instance.setattr("location", &location)?;
    let keywords: Bound<'py, PyDict> = [
        (
            "phase",
            native_enum(py, "DiagnosticPhase")?.getattr("COMPILATION")?,
        ),
        (
            "severity",
            native_enum(py, "DiagnosticSeverity")?.getattr("ERROR")?,
        ),
        ("code", "SQT-COMPILE-001".into_pyobject(py)?.into_any()),
        ("message", message),
        (
            "location",
            location.unwrap_or_else(|| py.None().into_bound(py)),
        ),
        ("related_locations", related_locations.into_bound(py)),
    ]
    .into_py_dict(py)?;
    let diagnostic: Bound<'py, PyAny> = py
        .get_type::<CompilerDiagnostic>()
        .call((), Some(&keywords))?;
    instance.setattr("diagnostic", diagnostic)
}

/// Plain exceptions: public module, name, docstring and the registered parent.
const PLAIN_ERRORS: [(&str, &str, &str, &str); 20] = [
    (
        "sqltide.auth.exceptions",
        "AuthenticationError",
        "A request could not produce an authenticated principal.",
        "Exception",
    ),
    (
        "sqltide.auth.exceptions",
        "AuthorizationError",
        "An authenticated principal cannot perform an account operation.",
        "Exception",
    ),
    (
        "sqltide.auth.exceptions",
        "ControlStoreError",
        "The account control store is unavailable or incompatible.",
        "Exception",
    ),
    (
        "sqltide.auth.exceptions",
        "AuthConfigurationError",
        "Authentication runtime configuration is invalid.",
        "ValueError",
    ),
    (
        "sqltide.auth.exceptions",
        "AccountValidationError",
        "Account input violates a stable domain constraint.",
        "ValueError",
    ),
    (
        "sqltide.auth.exceptions",
        "PasswordValidationError",
        "A password does not meet the configured security policy.",
        "ValueError",
    ),
    (
        "sqltide.auth.exceptions",
        "AccountConflictError",
        "An account mutation conflicts with existing identity state.",
        "ControlStoreError",
    ),
    (
        "sqltide.auth.exceptions",
        "AccountNotFoundError",
        "An account or role assignment does not exist.",
        "ControlStoreError",
    ),
    (
        "sqltide.cli.compile.exceptions",
        "CompileArtifactError",
        "Raised when a compile artifact cannot be generated or published safely.",
        "ValueError",
    ),
    (
        "sqltide.sensors.exceptions",
        "SensorStepError",
        "Raised when a durable step cannot run or return a memoized result.",
        "RuntimeError",
    ),
    (
        "sqltide.sensors.exceptions",
        "SensorEvaluationTimeoutError",
        "Raised when one sensor evaluation exceeds its timeout.",
        "RuntimeError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "DevServerError",
        "Raised when the dev server cannot satisfy a request.",
        "RuntimeError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "DevConfigurationError",
        "Raised when retained dev server configuration is internally inconsistent.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "KafkaCollectorClosedError",
        "Raised when Kafka metadata is requested after its retained clients close.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "ProjectNotCompiledError",
        "Raised when a request needs definitions but the project compile is failing.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "BuildInProgressError",
        "Raised when a build is requested while another one is still running.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "BuildStartError",
        "Raised when a spawned build exits or stalls before its run_started event.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "AuditSchedulerPersistenceError",
        "Raised when a scheduled slot cannot be durably marked as attempted.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "MessageQueryValidationError",
        "Raised when a message browsing document names an unsupported field or value.",
        "DevServerError",
    ),
    (
        "sqltide.dev_server.exceptions",
        "MessageSchemaError",
        "Raised when a raw landing table predates the header-array landing schema.",
        "DevServerError",
    ),
];

fn exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    for (public_module, name, doc, parent) in PLAIN_ERRORS {
        let parent: Bound<'_, PyAny> = match parent {
            "Exception" => py.get_type::<PyException>().into_any(),
            "ValueError" => py.get_type::<PyValueError>().into_any(),
            "RuntimeError" => py.get_type::<PyRuntimeError>().into_any(),
            registered => module.getattr(registered)?,
        };
        exception(module, public_module, name, doc, &[parent], None, None)?;
    }
    let cli_user: Bound<'_, PyAny> = exception(
        module,
        "sqltide.cli.entry.exceptions",
        "CliUserError",
        "Expected CLI-facing error that should be rendered without a traceback.",
        &[py.get_type::<PyException>().into_any()],
        Some(wrap_pyfunction!(cli_user_init, module)?.into_any()),
        None,
    )?;
    exception(
        module,
        "sqltide.cli.build.exceptions",
        "BuildPipelineLimitError",
        "The final build closure exceeds its committed target limit.",
        &[cli_user],
        None,
        None,
    )?;
    exception(
        module,
        "sqltide.sensors.exceptions",
        "SensorError",
        "Raised when sensor definitions or project sensor state is invalid.",
        &[py.get_type::<PyValueError>().into_any()],
        Some(wrap_pyfunction!(sensor_init, module)?.into_any()),
        None,
    )?;
    Ok(())
}

const TEXT: [(&str, &str); 14] = [
    ("CONTROL_STORE_ENV_VAR", "SQLTIDE_CONTROL_STORE_URL"),
    (
        "EXPECTED_BUILD_WRITE_SCOPE_ENV_VAR",
        "SQLTIDE_EXPECTED_BUILD_WRITE_SCOPE",
    ),
    (
        "EXPECTED_BUILD_READ_SCOPE_ENV_VAR",
        "SQLTIDE_EXPECTED_BUILD_READ_SCOPE",
    ),
    (
        "EXPECTED_BUILD_PIPELINE_SCOPE_ENV_VAR",
        "SQLTIDE_EXPECTED_BUILD_PIPELINE_SCOPE",
    ),
    ("DEV_CLI_VARIABLES_ENV_VAR", "SQLTIDE_INTERNAL_CLI_VARS"),
    ("METADATA_TABLE_NAME_PREFIX", "sqltide_"),
    ("ADMIN_ROLE", "admin"),
    ("VIEWER_ROLE", "viewer"),
    ("ACCOUNT_SCHEMA_COMPONENT", "accounts"),
    ("SQLITE_MEMORY_PATH", ":memory:"),
    ("RECOVER_MODEL_VIEW_STEP_PREFIX", "recover_view_"),
    ("RECOVER_SOURCE_STEP_PREFIX", "recover_source_"),
    ("RECOVER_DEAD_LETTERS_STEP_PREFIX", "recover_dead_letters_"),
    ("DEFAULT_DEV_SERVER_HOST", "127.0.0.1"),
];

fn constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py: Python<'_> = module.py();
    for (name, value) in TEXT {
        add(module, name, value)?;
    }
    add(
        module,
        "COMMANDS_REQUIRING_PIPELINES_ROOT",
        member_set(
            py,
            "CliCommand",
            &[
                "DEV",
                "DISCOVER",
                "COMPILE",
                "LINT",
                "FORMAT",
                "TEST",
                "PLAN",
                "BUILD",
                "DESTROY",
                "RESET_TARGET",
                "DEPLOYMENT",
                "RECONCILE",
                "RENAME_PROJECT",
                "AUDIT",
            ],
        )?,
    )?;
    add(
        module,
        "COMMANDS_WITHOUT_ADAPTER_CONNECTION",
        member_set(py, "CliCommand", &["DISCOVER", "COMPILE", "LINT", "FORMAT"])?,
    )?;
    add(
        module,
        "VIRTUAL_ENVIRONMENT_ONLY_COMMANDS",
        member_set(
            py,
            "CliCommand",
            &["DEPLOYMENT", "RECONCILE", "JANITOR", "DOCTOR", "REPAIR"],
        )?,
    )?;
    let command: Bound<'_, PyAny> = native_enum(py, "CliCommand")?;
    let display_names: Bound<'_, PyDict> = PyDict::new(py);
    for (member, display) in [
        ("AUDIT", "audit"),
        ("DESTROY", "destroy"),
        ("REPAIR", "repair active-view"),
        ("RESET_TARGET", "reset-target"),
    ] {
        display_names.set_item(command.getattr(member)?, display)?;
    }
    add(module, "DISPLAY_NAME_BY_COMMAND", display_names)?;
    add(
        module,
        "AFFIRMATIVE_RESPONSES",
        PyFrozenSet::new(py, ["y", "yes"])?,
    )?;
    add(
        module,
        "TRUE_ENV_VALUES",
        PyFrozenSet::new(py, ["1", "true", "yes"])?,
    )?;
    add(module, "PASSWORD_MIN_LENGTH", 12)?;
    add(
        module,
        "LOCAL_USER_ID",
        py.import("uuid")?
            .getattr("UUID")?
            .call1(("a25809d6-cf14-592c-b7b6-c94ef048e0cc",))?,
    )?;
    add(module, "DEFAULT_SENSOR_TIMEOUT_SECONDS", 60.0)?;
    add(module, "DEFAULT_DEV_SERVER_PORT", 8000)?;
    add(module, "KAFKA_REUSE_LOG_FIRST_READ_COUNT", 2)?;
    add(module, "KAFKA_REUSE_LOG_INTERVAL", 100)?;
    add(module, "CANCEL_GRACE_SECONDS", 15.0)?;
    add(module, "TERMINATE_GRACE_SECONDS", 3.0)?;
    add(module, "ACTIVITY_WINDOW_SECONDS", 120)
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    exceptions(module)?;
    constants(module)?;
    add_class::<SourceLocation>(module)?;
    add_class::<RelatedDiagnosticLocation>(module)?;
    add_class::<CompilerDiagnostic>(module)?;
    add_class::<Principal>(module)?;
    add_class::<AuthSettings>(module)?;
    add_class::<UserAccount>(module)?;
    add_class::<SessionCredentials>(module)?;
    add_class::<ResolvedSession>(module)?;
    add_class::<AuthenticatedRequest>(module)?;
    add_class::<AccountAuditRecord>(module)?;
    add_class::<ProjectRoleAssignment>(module)?;
    add_class::<AuthorizationRequest>(module)?;
    add_class::<AuthorizationDecision>(module)?;
    add_class::<CapabilityRequest>(module)?;
    add_class::<EffectiveCapabilities>(module)?;
    add_class::<WarehouseStatement>(module)?;
    add_class::<BuildWorkflow>(module)?;
    add_class::<PublishedBuildWorkflow>(module)?;
    add_class::<WorkflowStatementResult>(module)?;
    add_class::<WorkflowExecutionResult>(module)?;
    add_class::<BuildCommandOptions>(module)?;
    add_class::<BuildProtectionRequirement>(module)?;
    add_class::<WorkflowPreparationOptions>(module)?;
    add_class::<DirectBuildPreviewContext>(module)?;
    add_class::<VirtualBuildPreviewContext>(module)?;
    add_class::<DirectWorkflowPreparation>(module)?;
    add_class::<VirtualWorkflowPreparation>(module)?;
    add_class::<MixedWorkflowPreparation>(module)?;
    add_class::<DestructionCommandOptions>(module)?;
    add_class::<DevCommandOptions>(module)?;
    add_class::<CliEntrypointHandlers>(module)?;
    add_class::<PlanCommandOptions>(module)?;
    add_class::<SelectionResolution>(module)
}
