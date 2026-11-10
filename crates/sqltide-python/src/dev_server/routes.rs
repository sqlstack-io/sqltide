//! The dev server's API routes, in the order the application registers them.

use std::time::Instant;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use sqltide_json::{JsonKey, JsonValue};

use super::app::{BodySpec, Call, Endpoint, QueryParam};
use super::auth;
use super::authz;
use super::destruction;
use super::http::{Failure, HttpReply, Outcome, Route, object, py_json};
use super::sensors;
use super::support::{ADAPTER_ERROR, Scalar, attr, is_instance, translate};
use crate::handling::while_handling;
use crate::py_kwargs;

pub(crate) const DEV_EXCEPTIONS: &str = "sqltide.dev_server.exceptions";
pub(crate) const VALUE_ERROR: (&str, &str, u16) = ("builtins", "ValueError", 400);
const DEV_MODELS: &str = "sqltide.dev_server.models";
const AUTH_MODELS: &str = "sqltide.auth.models";
const STATE_PAYLOAD: &str = "sqltide.dev_server._helpers.payloads.state_payload";
const DEFINITIONS_PAYLOAD: &str = "sqltide.dev_server._helpers.payloads.definitions_payload";
const DEPLOYMENTS_PAYLOAD: &str = "sqltide.dev_server._helpers.payloads.deployments_payload";
const PLAN_PAYLOAD: &str = "sqltide.dev_server._helpers.payloads.plan_payload";
const MESSAGE_QUERY: &str = "sqltide.dev_server._helpers.queries.message_query";
const RUNS_QUERY: &str = "sqltide.dev_server._helpers.queries.runs_query";
const CHECKS: &str = "sqltide.dev_server._helpers.server.checks_execution";
const COMPILE_RUNNER: &str = "sqltide.dev_server._helpers.server.compile_runner";
const DEPLOYMENT_OPERATIONS: &str = "sqltide.dev_server._helpers.server.deployment_operations";
const BUILD_PROCESS: &str = "sqltide.dev_server.classes.build_process";
const NATIVE: &str = "sqltide._native";

const TEXT: Scalar = Scalar::Text { min_length: None };
const NON_NEGATIVE: Scalar = Scalar::Integer { ge: Some(0), le: None };
const INTEGER: Scalar = Scalar::Integer { ge: None, le: None };

/// The failures the build and plan routes answer with 400.
const PLANNING_ERRORS: [(&str, &str, u16); 5] = [
    ("sqltide.executor.backfill.exceptions", "BackfillExecutionError", 400),
    ("sqltide.cli.entry.exceptions", "CliUserError", 400),
    ("sqltide.executor.direct.exceptions", "DirectBuildError", 400),
    ("sqltide.compiler.planner.exceptions", "DirectPlanError", 400),
    VALUE_ERROR,
];

// Shared request plumbing.

pub(crate) fn database(call: &Call<'_, '_>) -> String {
    call.app.services.database.clone().unwrap_or_default()
}

pub(crate) fn state<'py>(call: &Call<'py, '_>) -> Bound<'py, PyAny> {
    call.service(|services| &services.state)
}

pub(crate) fn warehouse<'py>(call: &Call<'py, '_>) -> Bound<'py, PyAny> {
    call.service(|services| &services.warehouse)
}

/// Call `module.name(**kwargs)`, looked up when called.
pub(crate) fn invoke<'py>(py: Python<'py>, module: &str, name: &str, kwargs: Bound<'py, PyDict>) -> PyResult<Bound<'py, PyAny>> {
    attr(py, module, name)?.call((), Some(&kwargs))
}

/// The latest valid analysis, or 409 while the project compile is failing.
pub(crate) fn servable<'py>(call: &Call<'py, '_>) -> Outcome<Bound<'py, PyAny>> {
    match state(call).call_method0("current_analysis") {
        Ok(analysis) => Ok(analysis),
        Err(error) => Err(translate(call.py, error, &[(DEV_EXCEPTIONS, "ProjectNotCompiledError", 409)])),
    }
}

/// The servable analysis, or `None` while the compile is failing.
pub(crate) fn optional_servable<'py>(call: &Call<'py, '_>) -> Outcome<Option<Bound<'py, PyAny>>> {
    match servable(call) {
        Ok(analysis) => Ok(Some(analysis)),
        Err(Failure::Http { .. }) => Ok(None),
        Err(other) => Err(other),
    }
}

fn no_connection() -> Failure {
    Failure::status(503, "no warehouse connection")
}

/// The shared warehouse client, or 503 without one.
pub(crate) fn required_connection<'py>(call: &Call<'py, '_>) -> Outcome<Bound<'py, PyAny>> {
    let connection = warehouse(call).getattr("connection")?;
    if connection.is_none() || call.app.services.database.is_none() {
        return Err(no_connection());
    }
    Ok(connection)
}

/// `with manager as entered: body(entered)`; the managers here never suppress.
pub(crate) fn within<'py, T>(
    manager: &Bound<'py, PyAny>,
    body: impl FnOnce(&Bound<'py, PyAny>) -> Outcome<T>,
) -> Outcome<T> {
    let py: Python<'py> = manager.py();
    let kind = manager.get_type();
    let enter = kind.getattr("__enter__")?;
    let exit = kind.getattr("__exit__")?;
    let entered = enter.call1((manager,))?;
    match body(&entered) {
        Ok(value) => {
            exit.call1((manager, py.None(), py.None(), py.None()))?;
            Ok(value)
        }
        Err(Failure::Python(error)) => {
            let raised = error.value(py).clone().into_any();
            let traceback = raised.getattr("__traceback__")?;
            if let Err(exit_error) = exit.call1((manager, raised.get_type(), &raised, traceback)) {
                return Err(Failure::Python(while_handling(py, exit_error, &error)));
            }
            Err(Failure::Python(error))
        }
        Err(other) => {
            exit.call1((manager, py.None(), py.None(), py.None()))?;
            Err(other)
        }
    }
}

/// Run `body` holding the shared warehouse query lock.
pub(crate) fn locked<'py, T>(call: &Call<'py, '_>, body: impl FnOnce() -> Outcome<T>) -> Outcome<T> {
    within(&state(call).getattr("query_lock")?, |_| body())
}

/// Run `body` on an isolated bounded read connection, or 503 without one.
pub(crate) fn reading<'py, T>(call: &Call<'py, '_>, body: impl FnOnce(&Bound<'py, PyAny>) -> Outcome<T>) -> Outcome<T> {
    let manager = warehouse(call).call_method0("read_connection")?;
    let has_database: bool = call.app.services.database.is_some();
    within(&manager, |connection| {
        if connection.is_none() || !has_database {
            return Err(Failure::status(503, "no warehouse read connection"));
        }
        body(connection)
    })
}

/// Map Python failures of `result` through ordered `except` clauses.
pub(crate) fn catching<T>(py: Python<'_>, result: Outcome<T>, clauses: &[(&str, &str, u16)]) -> Outcome<T> {
    match result {
        Err(Failure::Python(error)) => Err(translate(py, error, clauses)),
        other => other,
    }
}

fn status_payload<'py>(call: &Call<'py, '_>, outcome: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    let warehouse_status = warehouse(call).call_method0("status")?;
    invoke(py, COMPILE_RUNNER, "build_status_payload", py_kwargs!(py; outcome = outcome, warehouse_status = warehouse_status)?)
}

// Status, state and project routes.

fn read_status(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let warehouse_status = warehouse(call).call_method0("status")?;
    let outcome = state(call).call_method0("current")?;
    let payload = invoke(py, COMPILE_RUNNER, "build_status_payload", py_kwargs!(py; outcome = outcome, warehouse_status = warehouse_status)?)?;
    call.payload(&payload)
}

fn refresh_warehouse(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let runtime = warehouse(call);
    if runtime.getattr("connection")?.is_none() {
        runtime.call_method0("connect_now")?;
    }
    state(call).getattr("snapshot")?.call_method0("invalidate")?;
    read_status(call)
}

fn read_state(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    if warehouse(call).getattr("connection")?.is_none() || call.app.services.database.is_none() {
        return Err(no_connection());
    }
    let result: Outcome<Bound<'_, PyAny>> = state(call)
        .getattr("snapshot")
        .and_then(|snapshot| snapshot.call_method0("current"))
        .map_err(Failure::from);
    let payload = catching(call.py, result, &[(DEV_EXCEPTIONS, "ProjectNotCompiledError", 409), ADAPTER_ERROR])?;
    call.payload(&payload)
}

fn elapsed_ms(from: Instant, to: Instant) -> i128 {
    i128::try_from(to.duration_since(from).as_millis()).unwrap_or(i128::MAX)
}

fn preparation_options<'py>(
    call: &Call<'py, '_>,
    selectors: &Bound<'py, PyTuple>,
    deployment_id: Bound<'py, PyAny>,
    start_time: Bound<'py, PyAny>,
    changed: bool,
    include_missing_upstream: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    let database = call.app.services.database.clone();
    invoke(
        py,
        "sqltide.cli.build.models",
        "WorkflowPreparationOptions",
        py_kwargs!(py;
            database = database.clone(),
            metadata_database = database,
            selectors = selectors,
            deployment_id = deployment_id,
            full_refresh = false,
            start_time = start_time,
            verbose = false,
            changed = changed,
            include_missing_upstream = include_missing_upstream,
        )?,
    )
}

fn resolved_deployment_id<'py>(preparation: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = preparation.py();
    if preparation.is_instance(&attr(py, "sqltide.cli.build.models", "VirtualWorkflowPreparation")?)? {
        return preparation.getattr("preview")?.getattr("deployment_id");
    }
    if preparation.is_instance(&attr(py, "sqltide.cli.build.models", "MixedWorkflowPreparation")?)? {
        return preparation.getattr("virtual")?.getattr("preview")?.getattr("deployment_id");
    }
    Ok(py.None().into_bound(py))
}

fn read_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let started: Instant = Instant::now();
    let select: Vec<String> = call.query_list(0).unwrap_or_default();
    let start: Option<String> = call.query_text(1);
    let deployment: Option<String> = call.query_text(2);
    let counts: bool = call.query_flag(3);
    let changed: bool = call.query_flag(4);
    let include_missing_upstream: bool = call.query_flag(5);
    let result: Outcome<Bound<'_, PyAny>> = (|| {
        if start.is_some() && select.is_empty() && !changed {
            return Err(Failure::status(400, "--start-time requires --changed or at least one --select"));
        }
        reading(call, |client| {
            let acquired: Instant = Instant::now();
            let planned_at = client.call_method0("capture_warehouse_timestamp")?;
            let selectors = PyTuple::new(py, &select)?;
            let options = preparation_options(
                call,
                &selectors,
                pyo3::IntoPyObjectExt::into_bound_py_any(deployment.clone(), py)?,
                pyo3::IntoPyObjectExt::into_bound_py_any(start.clone(), py)?,
                changed,
                include_missing_upstream,
            )?;
            let preparation = invoke(
                py,
                NATIVE,
                "prepare_build_workflow",
                py_kwargs!(py; analysis = &analysis, options = options, client = client, adapter_profile = analysis.getattr("adapter_profile")?)?,
            )?;
            invoke(py, NATIVE, "validate_build_pipeline_limit", py_kwargs!(py; analysis = &analysis, preparation = &preparation)?)?;
            let direct = authz::direct_part(&preparation)?;
            let invocation = invoke(
                py,
                BUILD_PROCESS,
                "build_invocation",
                py_kwargs!(py;
                    selectors = &selectors,
                    start_time = start.clone(),
                    changed = changed,
                    include_missing_upstream = include_missing_upstream,
                    deployment_id = resolved_deployment_id(&preparation)?,
                    execution_context = call.service(|services| &services.execution_context),
                )?,
            )?;
            let command = invocation.get_item(1)?;
            let planned: Instant = Instant::now();
            let replay_row_counts: Bound<'_, PyAny> = match (&direct, counts) {
                (Some(direct), true) => {
                    let preview = direct.getattr("preview")?;
                    invoke(
                        py,
                        PLAN_PAYLOAD,
                        "count_replay_rows",
                        py_kwargs!(py;
                            connection = client,
                            database = preview.getattr("database")?,
                            plan = preview.getattr("plan")?,
                            start_time = preview.getattr("effective_start_time")?,
                        )?,
                    )?
                }
                _ => PyDict::new(py).into_any(),
            };
            let payload = invoke(
                py,
                PLAN_PAYLOAD,
                "build_mode_aware_plan_payload",
                py_kwargs!(py;
                    preparation = &preparation,
                    analysis = &analysis,
                    selectors = &selectors,
                    planned_at = planned_at,
                    command = command,
                    replay_row_counts = replay_row_counts,
                )?,
            )?;
            let completed: Instant = Instant::now();
            let timings = PyDict::new(py);
            timings.set_item("connectionWaitMs", elapsed_ms(started, acquired))?;
            timings.set_item("planningMs", elapsed_ms(acquired, planned))?;
            timings.set_item("replayCountsMs", elapsed_ms(planned, completed))?;
            timings.set_item("totalMs", elapsed_ms(started, completed))?;
            payload.set_item("timings", timings)?;
            Ok(payload)
        })
    })();
    let mut clauses: Vec<(&str, &str, u16)> = PLANNING_ERRORS.to_vec();
    clauses.push(ADAPTER_ERROR);
    let payload = catching(py, result, &clauses)?;
    call.payload(&payload)
}

fn reload_project(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let state = state(call);
    // Authorize against the held policy snapshot and recompile under one compile lock.
    let outcome = within(&state.getattr("_compile_lock")?, |_| {
        let held = state.getattr("_authorization_analysis")?;
        authz::require_reload(call, (!held.is_none()).then_some(&held))?;
        Ok(state.call_method0("_reload_locked")?)
    })?;
    call.service(|services| &services.reporter)
        .call_method("report_reload", (), Some(&py_kwargs!(py; outcome = &outcome)?))?;
    call.payload(&status_payload(call, &outcome)?)
}

fn read_capabilities(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let analysis = optional_servable(call)?;
    call.payload_json(&authz::capabilities_payload(call, analysis.as_ref())?)
}

fn read_access_policy(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let analysis = optional_servable(call)?;
    call.payload_json(&authz::access_policy_payload(analysis.as_ref())?)
}

fn read_definitions(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let state = state(call);
    let resolved: PyResult<(Bound<'_, PyAny>, Bound<'_, PyAny>)> = (|| {
        let outcome = state.call_method0("current_servable_outcome")?;
        let analysis = state.call_method0("current_analysis")?;
        Ok((outcome, analysis))
    })();
    let (outcome, analysis) = resolved.map_err(|error| translate(py, error, &[(DEV_EXCEPTIONS, "ProjectNotCompiledError", 409)]))?;
    let version_key = outcome.getattr("version_key")?;
    let etag: String = format!("\"{}\"", version_key.str()?);
    if call.request.header("if-none-match") == Some(etag.as_str()) {
        return Ok(HttpReply {
            status: 304,
            headers: vec![("etag".to_owned(), etag)],
            body: Vec::new(),
        });
    }
    let payload = invoke(py, DEFINITIONS_PAYLOAD, "build_definitions_payload", py_kwargs!(py; analysis = analysis, version_key = version_key)?)?;
    let mut reply: HttpReply = HttpReply::json(200, &py_json(&payload)?)?;
    reply.headers.insert(0, ("etag".to_owned(), etag));
    Ok(reply)
}

fn read_topics(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let connection = warehouse(call).getattr("connection")?;
    let result = locked(call, || {
        Ok(invoke(
            py,
            STATE_PAYLOAD,
            "build_topics_payload",
            py_kwargs!(py;
                analysis = &analysis,
                connection = &connection,
                database = call.app.services.database.clone(),
                topic_reader = call.service(|services| &services.kafka_topic_reader),
                kafka_lag_reader = call.service(|services| &services.kafka_lag_reader),
            )?,
        )?)
    });
    let payload = catching(py, result, &[ADAPTER_ERROR])?;
    call.payload(&payload)
}

// Deployment routes.

fn read_deployments(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            DEPLOYMENTS_PAYLOAD,
            "build_deployments_payload",
            py_kwargs!(py; connection = client, database = database(call), metadata_database = database(call))?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

fn read_one_deployment(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let deployment_id: String = call.path[0].text().to_owned();
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            DEPLOYMENTS_PAYLOAD,
            "build_deployment_detail_payload",
            py_kwargs!(py;
                connection = client,
                database = database(call),
                metadata_database = database(call),
                deployment_id = deployment_id.as_str(),
            )?,
        )?)
    });
    let payload = catching(py, result, &[ADAPTER_ERROR, VALUE_ERROR])?;
    if payload.is_none() {
        return Err(Failure::status(404, format!("deployment '{deployment_id}' was not found")));
    }
    call.payload(&payload)
}

fn read_deployment_diff(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let deployment_id: &str = call.path[0].text();
    let comparison: String = match call.query_text(0) {
        Some(against) => format!("{against}:{deployment_id}"),
        None => deployment_id.to_owned(),
    };
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            DEPLOYMENT_OPERATIONS,
            "build_deployment_diff_payload",
            py_kwargs!(py;
                connection = client,
                database = database(call),
                metadata_database = database(call),
                comparison = comparison.as_str(),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR, VALUE_ERROR])?)
}

fn promote_deployment(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let deployment_id: String = call.path[0].text().to_owned();
    let client = required_connection(call)?;
    if !authz::is_system_admin(call)? {
        let analysis = servable(call)?;
        let logical_ids = locked(call, || {
            Ok(invoke(
                py,
                DEPLOYMENT_OPERATIONS,
                "promotion_executed_logical_ids",
                py_kwargs!(py; connection = &client, metadata_database = database(call), deployment_id = deployment_id.as_str())?,
            )?)
        })?;
        authz::require_promotion(call, &analysis, &deployment_id, &logical_ids)?;
    }
    let result = locked(call, || {
        Ok(invoke(
            py,
            DEPLOYMENT_OPERATIONS,
            "run_deployment_promotion",
            py_kwargs!(py;
                connection = &client,
                database = database(call),
                metadata_database = database(call),
                deployment_id = deployment_id.as_str(),
                project_dir = call.service(|services| &services.project_dir),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR, VALUE_ERROR])?)
}

fn cleanup_deployments(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = optional_servable(call)?;
    authz::require_cleanup(call, analysis.as_ref())?;
    let client = required_connection(call)?;
    let retention_days = call.body()?.getattr("retentionDays")?;
    let result = locked(call, || {
        Ok(invoke(
            py,
            DEPLOYMENT_OPERATIONS,
            "run_deployment_cleanup",
            py_kwargs!(py;
                connection = &client,
                database = database(call),
                metadata_database = database(call),
                retention_days = &retention_days,
                project_dir = call.service(|services| &services.project_dir),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR, VALUE_ERROR])?)
}

// Source message routes.

const MESSAGE_ERRORS: [(&str, &str, u16); 3] = [
    (DEV_EXCEPTIONS, "MessageQueryValidationError", 400),
    (DEV_EXCEPTIONS, "MessageSchemaError", 409),
    ADAPTER_ERROR,
];

/// The authorized analysis, the shared client and the landing relation of a Kafka source.
fn message_source<'py>(call: &Call<'py, '_>) -> Outcome<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py: Python<'py> = call.py;
    let analysis = servable(call)?;
    authz::require_message_read(call, &analysis)?;
    let client = required_connection(call)?;
    let name: &str = call.path[0].text();
    let mut found: Option<Bound<'py, PyAny>> = None;
    for candidate in analysis.getattr("compiled_project")?.getattr("sources")?.try_iter()? {
        let candidate = candidate?;
        if candidate.getattr("key")?.getattr("name")?.eq(name)? {
            found = Some(candidate);
            break;
        }
    }
    let Some(source) = found else {
        return Err(Failure::status(404, format!("unknown source '{name}'")));
    };
    let landing = attr(py, "sqltide.compiler.discovery.models", "KafkaLandingStep")?;
    if !source.getattr("source")?.is_instance(&landing)? {
        return Err(Failure::status(400, format!("source '{name}' is not a managed Kafka source")));
    }
    let relation = analysis
        .getattr("realized_project")?
        .getattr("relation_name_by_logical_key")?
        .get_item(source.getattr("key")?)?;
    Ok((analysis, client, relation))
}

fn ensure_headers(call: &Call<'_, '_>, client: &Bound<'_, PyAny>, relation: &Bound<'_, PyAny>) -> PyResult<()> {
    let py: Python<'_> = call.py;
    invoke(
        py,
        MESSAGE_QUERY,
        "ensure_header_columns",
        py_kwargs!(py; connection = client, database = database(call), relation_name = relation)?,
    )
    .map(drop)
}

fn read_messages(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    read_message_query(call, "read_source_messages")
}

fn read_message_facets(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    read_message_query(call, "read_source_message_facets")
}

fn read_message_query(call: &Call<'_, '_>, function: &str) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let (_analysis, client, relation) = message_source(call)?;
    let request = call.body()?;
    let result = locked(call, || {
        ensure_headers(call, &client, &relation)?;
        Ok(invoke(
            py,
            MESSAGE_QUERY,
            function,
            py_kwargs!(py; connection = &client, database = database(call), relation_name = &relation, request = &request)?,
        )?)
    });
    call.payload(&catching(py, result, &MESSAGE_ERRORS)?)
}

fn read_message_record(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let (_analysis, client, relation) = message_source(call)?;
    let request = call.body()?;
    let partition = request.getattr("partition")?;
    let offset = request.getattr("offset")?;
    let result = locked(call, || {
        ensure_headers(call, &client, &relation)?;
        Ok(invoke(
            py,
            MESSAGE_QUERY,
            "read_source_message_record",
            py_kwargs!(py;
                connection = &client,
                database = database(call),
                relation_name = &relation,
                partition = &partition,
                offset = &offset,
            )?,
        )?)
    });
    let record = catching(py, result, &MESSAGE_ERRORS)?;
    if record.is_none() {
        return Err(Failure::status(
            404,
            format!(
                "no message at partition {} offset {} in '{}'",
                partition.str()?,
                offset.str()?,
                call.path[0].text()
            ),
        ));
    }
    call.payload(&record)
}

// Check, audit and run-history routes.

const CHECK_ERRORS: [(&str, &str, u16); 2] = [(DEV_EXCEPTIONS, "DevServerError", 400), ADAPTER_ERROR];

fn run_check(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let request = call.body()?;
    let kind: String = request.getattr("kind")?.extract()?;
    let name: String = request.getattr("name")?.extract()?;
    let runner: &str = match kind.as_str() {
        "audit" => "run_one_audit",
        "test" => "run_one_test",
        _ => return Err(Failure::status(400, "kind must be 'audit' or 'test'")),
    };
    authz::require_check(call, &analysis, &kind, &name)?;
    let client = required_connection(call)?;
    let result = locked(call, || {
        Ok(invoke(
            py,
            CHECKS,
            runner,
            py_kwargs!(py;
                analysis = &analysis,
                connection = &client,
                name = name.as_str(),
                project_dir = call.service(|services| &services.project_dir),
                database = database(call),
            )?,
        )?)
    });
    let payload = catching(py, result, &CHECK_ERRORS)?;
    let deferred: bool = !payload.call_method1("get", ("deferredUntil",))?.is_none();
    let passed: bool = payload.call_method1("get", ("passed",))?.is_truthy()?;
    let tone = attr(py, "sqltide.dev_server.types", "ActivityTone")?.getattr(if deferred {
        "CAUTION"
    } else if passed {
        "GOOD"
    } else {
        "BAD"
    })?;
    let status: &str = if deferred {
        "deferred"
    } else if passed {
        "pass"
    } else {
        "fail"
    };
    call.service(|services| &services.reporter).call_method(
        "report_activity",
        (),
        Some(&py_kwargs!(py; category = kind.as_str(), status = status, tone = tone, detail = name.as_str())?),
    )?;
    call.payload(&payload)
}

fn run_audits(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let request = call.body()?;
    let analysis = servable(call)?;
    let connection = required_connection(call)?;
    let names: Vec<String> = request.getattr("names")?.extract()?;
    for name in &names {
        authz::require_check(call, &analysis, "audit", name)?;
    }
    let result = locked(call, || {
        Ok(invoke(
            py,
            CHECKS,
            "run_audit_batch",
            py_kwargs!(py;
                analysis = &analysis,
                connection = &connection,
                names = PyTuple::new(py, &names)?,
                project_dir = call.service(|services| &services.project_dir),
                database = database(call),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &CHECK_ERRORS)?)
}

fn read_checks_status(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let client = required_connection(call)?;
    let analysis = servable(call)?;
    let result = locked(call, || {
        Ok(invoke(
            py,
            CHECKS,
            "build_checks_status_payload",
            py_kwargs!(py;
                analysis = &analysis,
                connection = &client,
                database = database(call),
                project_dir = call.service(|services| &services.project_dir),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

fn read_audit_scheduler(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let client = required_connection(call)?;
    let health = call.service(|services| &services.audit_scheduler).call_method0("health")?;
    let health_state: String = health.get_item("state")?.str()?.to_string();
    if health_state == "running" || health_state == "backing_off" {
        return call.payload_json(&object(vec![
            ("enabled", JsonValue::Bool(true)),
            ("state", py_json(&health.get_item("state")?)?),
            ("warehouseNow", py_json(&health.get_item("lastSuccessfulTick")?)?),
            ("dueCount", py_json(&health.get_item("runningAuditCount")?)?),
            ("audits", JsonValue::Array(Vec::new())),
            ("health", py_json(&health)?),
        ]));
    }
    let analysis = servable(call)?;
    let result = locked(call, || {
        let payload = invoke(
            py,
            "sqltide.dev_server.main._build_audit_scheduler_payload",
            "build_audit_scheduler_payload",
            py_kwargs!(py;
                analysis = &analysis,
                connection = &client,
                database = database(call),
                project_dir = call.service(|services| &services.project_dir),
            )?,
        )?;
        payload.set_item("health", &health)?;
        Ok(payload)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

fn presumed_failed_after(call: &Call<'_, '_>) -> i64 {
    call.app.services.presumed_failed_after_seconds
}

fn read_run_history(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            RUNS_QUERY,
            "read_runs",
            py_kwargs!(py;
                connection = client,
                database = database(call),
                presumed_failed_after_seconds = presumed_failed_after(call),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

fn read_one_run_events(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            RUNS_QUERY,
            "read_run_events",
            py_kwargs!(py;
                connection = client,
                database = database(call),
                invocation_id = call.path[0].text(),
                after = call.query_int(0, 0),
                presumed_failed_after_seconds = presumed_failed_after(call),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

fn read_one_run_statement(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let result = reading(call, |client| {
        Ok(invoke(
            py,
            RUNS_QUERY,
            "read_run_statement",
            py_kwargs!(py;
                connection = client,
                database = database(call),
                invocation_id = call.path[0].text(),
                statement_sequence = call.path[1].integer(),
            )?,
        )?)
    });
    call.payload(&catching(py, result, &[ADAPTER_ERROR])?)
}

// Build routes.

/// `None` unless `value` is a list of strings.
fn logical_id_scope(value: &Bound<'_, PyAny>) -> PyResult<Option<std::collections::HashSet<String>>> {
    if !value.is_instance_of::<PyList>() {
        return Ok(None);
    }
    let mut scope = std::collections::HashSet::new();
    for item in value.try_iter()? {
        let item = item?;
        let Ok(text) = item.cast::<PyString>() else {
            return Ok(None);
        };
        scope.insert(text.to_str()?.to_owned());
    }
    Ok(Some(scope))
}

fn string_set(value: &Bound<'_, PyAny>) -> PyResult<std::collections::HashSet<String>> {
    value.try_iter()?.map(|item| item?.extract::<String>()).collect()
}

/// Conservatively detect read/write conflicts between an active run and the requested build.
fn overlaps(
    run: &Bound<'_, PyAny>,
    writes: &std::collections::HashSet<String>,
    reads: &std::collections::HashSet<String>,
) -> PyResult<bool> {
    let active_writes = logical_id_scope(&run.call_method1("get", ("executedLogicalIds",))?)?;
    let active_reads = logical_id_scope(&run.call_method1("get", ("contextLogicalIds",))?)?;
    let (Some(active_writes), Some(active_reads)) = (active_writes, active_reads) else {
        return Ok(true);
    };
    Ok(!active_writes.is_disjoint(writes) || !active_writes.is_disjoint(reads) || !active_reads.is_disjoint(writes))
}

fn blocking_run_message(run: &Bound<'_, PyAny>, presumed_failed_after_seconds: i64) -> PyResult<String> {
    let py: Python<'_> = run.py();
    let invocation_id: String = run.get_item("invocationId")?.str()?.to_string();
    let status = run.get_item("status")?;
    let age_text = run.get_item("lastSignalAgeSeconds")?.str()?;
    let signal_age_seconds: i64 = py.get_type::<pyo3::types::PyInt>().call1((age_text,))?.extract()?;
    let status_text: String = status.str()?.to_string();
    if status_text == "unresponsive" {
        let retry_seconds: i64 = (presumed_failed_after_seconds - signal_age_seconds).max(0);
        return Ok(format!(
            "Run {invocation_id} is unresponsive: no signal for {signal_age_seconds}s. \
             No new run was started. To prevent overlapping warehouse writes, SQLTide \
             will wait {retry_seconds}s more before treating it as presumed failed \
             (configured safety window: {presumed_failed_after_seconds}s via \
             defaults.run_presumed_failed_after). Retry after that."
        ));
    }
    Ok(format!(
        "Run {invocation_id} is still {status_text} (last signal {signal_age_seconds}s ago), \
         so no new run was started. Wait for it to finish or cancel it from Runs."
    ))
}

fn read_active_runs<'py>(call: &Call<'py, '_>, client: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    invoke(
        py,
        RUNS_QUERY,
        "read_active_runs",
        py_kwargs!(py;
            connection = client,
            database = database(call),
            presumed_failed_after_seconds = presumed_failed_after(call),
        )?,
    )
}

fn start_build(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let request = call.body()?;
    let result: Outcome<Bound<'_, PyAny>> = locked(call, || {
        let client = required_connection(call)?;
        let analysis = servable(call)?;
        let needs_authorization: bool = !authz::is_system_admin(call)?;
        let changed: bool = request.getattr("changed")?.is_truthy()?;
        let include_missing_upstream: bool = request.getattr("includeMissingUpstream")?.is_truthy()?;
        let dynamic_selection: bool = changed || include_missing_upstream;
        let selectors = PyTuple::new(py, request.getattr("selectors")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
        if !dynamic_selection {
            invoke(py, NATIVE, "validate_build_pipeline_limit", py_kwargs!(py; analysis = &analysis, selectors = &selectors)?)?;
        }
        let mut active_runs: Vec<Bound<'_, PyAny>> = Vec::new();
        for run in read_active_runs(call, &client)?.try_iter()? {
            let run = run?;
            let status: String = run.get_item("status")?.str()?.to_string();
            if status == "running" || status == "unresponsive" {
                active_runs.push(run);
            }
        }
        let mut preparation: Option<Bound<'_, PyAny>> = None;
        if needs_authorization || !active_runs.is_empty() || dynamic_selection {
            let options = preparation_options(
                call,
                &selectors,
                request.getattr("deploymentId")?,
                request.getattr("startTime")?,
                changed,
                include_missing_upstream,
            )?;
            preparation = Some(invoke(
                py,
                NATIVE,
                "prepare_build_workflow",
                py_kwargs!(py; analysis = &analysis, options = options, client = &client, adapter_profile = analysis.getattr("adapter_profile")?)?,
            )?);
        }
        if let (true, Some(preparation)) = (dynamic_selection, &preparation) {
            invoke(py, NATIVE, "validate_build_pipeline_limit", py_kwargs!(py; analysis = &analysis, preparation = preparation)?)?;
        }
        if let (true, Some(preparation)) = (needs_authorization, &preparation) {
            authz::require_prepared_build(call, &analysis, preparation)?;
        }
        if let (false, Some(preparation)) = (active_runs.is_empty(), &preparation) {
            let scopes = attr(py, NATIVE, "resolve_prepared_build_scope")?.call1((preparation,))?;
            let writes = string_set(&scopes.get_item(0)?)?;
            let reads = string_set(&scopes.get_item(1)?)?;
            for run in &active_runs {
                if overlaps(run, &writes, &reads)? {
                    return Err(Failure::status(409, blocking_run_message(run, presumed_failed_after(call))?));
                }
            }
        }
        let none = || py.None().into_bound(py);
        let (expected_write_scope, expected_read_scope, expected_pipeline_scope) = match &preparation {
            Some(preparation) => {
                let scopes = attr(py, NATIVE, "resolve_prepared_build_scope")?.call1((preparation,))?;
                let pipelines = invoke(py, NATIVE, "prepared_pipeline_scope", py_kwargs!(py; preparation = preparation, analysis = &analysis)?)?;
                (scopes.get_item(0)?, scopes.get_item(1)?, pipelines)
            }
            None => (none(), none(), none()),
        };
        let confirmations = PyTuple::new(py, request.getattr("confirmations")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
        Ok(call.service(|services| &services.builds).call_method(
            "start",
            (),
            Some(&py_kwargs!(py;
                project_dir = call.service(|services| &services.project_dir),
                selectors = &selectors,
                start_time = request.getattr("startTime")?,
                changed = changed,
                include_missing_upstream = include_missing_upstream,
                deployment_id = request.getattr("deploymentId")?,
                confirmations = confirmations,
                expected_write_scope = expected_write_scope,
                expected_read_scope = expected_read_scope,
                expected_pipeline_scope = expected_pipeline_scope,
            )?),
        )?)
    });
    let mut clauses: Vec<(&str, &str, u16)> = vec![
        (DEV_EXCEPTIONS, "BuildInProgressError", 409),
        (DEV_EXCEPTIONS, "BuildStartError", 400),
    ];
    clauses.extend(PLANNING_ERRORS);
    clauses.push(ADAPTER_ERROR);
    call.payload(&catching(py, result, &clauses)?)
}

fn read_build_feed(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let feed = call
        .service(|services| &services.builds)
        .call_method("feed", (), Some(&py_kwargs!(py; after = call.query_int(0, 0))?))?;
    call.payload(&feed)
}

fn invocation_id_of(call: &Call<'_, '_>) -> PyResult<String> {
    call.body()?.call_method1("get", ("invocationId", ""))?.extract()
}

fn cancel_build(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let invocation_id: String = invocation_id_of(call)?;
    if !authz::is_system_admin(call)? {
        let client = required_connection(call)?;
        let analysis = servable(call)?;
        let active_runs = locked(call, || Ok(read_active_runs(call, &client)?))?;
        authz::require_run_cancel(call, &analysis, &invocation_id, &active_runs)?;
    }
    let result = call
        .service(|services| &services.builds)
        .call_method("cancel", (), Some(&py_kwargs!(py; invocation_id = invocation_id.as_str())?))
        .map_err(Failure::from);
    call.payload(&catching(py, result, &[(DEV_EXCEPTIONS, "BuildInProgressError", 409)])?)
}

fn kill_build(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = optional_servable(call)?;
    authz::require_kill(call, analysis.as_ref())?;
    let invocation_id: String = invocation_id_of(call)?;
    let result = call
        .service(|services| &services.builds)
        .call_method("kill", (), Some(&py_kwargs!(py; invocation_id = invocation_id.as_str())?))
        .map_err(Failure::from);
    call.payload(&catching(py, result, &[(DEV_EXCEPTIONS, "BuildInProgressError", 409)])?)
}

// Bootstrap.

fn read_bootstrap(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let state = state(call);
    let outcome = state.call_method0("current")?;
    let resolved: PyResult<(Bound<'_, PyAny>, Bound<'_, PyAny>)> = (|| {
        let servable = state.call_method0("current_servable_outcome")?;
        let analysis = state.call_method0("current_analysis")?;
        let definitions = invoke(
            py,
            DEFINITIONS_PAYLOAD,
            "build_definitions_payload",
            py_kwargs!(py; analysis = &analysis, version_key = servable.getattr("version_key")?)?,
        )?;
        Ok((analysis, definitions))
    })();
    let (analysis, definitions): (Option<Bound<'_, PyAny>>, JsonValue) = match resolved {
        Ok((analysis, definitions)) => (Some(analysis), py_json(&definitions)?),
        Err(error) if is_instance(py, &error, DEV_EXCEPTIONS, "ProjectNotCompiledError")? => (None, JsonValue::Null),
        Err(error) => return Err(error.into()),
    };
    let authenticated = auth::authenticated(call)?;
    let mut auth_payload: Vec<(&str, JsonValue)> = call.app.auth.browser_payload(py, &authenticated)?;
    auth_payload.push(("capabilities", authz::capabilities_payload(call, analysis.as_ref())?));
    let status = status_payload(call, &outcome)?;
    let held = state.getattr("snapshot")?.call_method0("held")?;
    call.payload_json(&object(vec![
        ("auth", object(auth_payload)),
        ("status", py_json(&status)?),
        ("definitions", definitions),
        ("state", py_json(&held)?),
    ]))
}

/// Replace values of existing keys in place, as `{**payload, key: value}` does.
pub(crate) fn merged(payload: JsonValue, updates: Vec<(&str, JsonValue)>) -> JsonValue {
    let JsonValue::Object(mut entries) = payload else {
        return payload;
    };
    for (key, value) in updates {
        let key: JsonKey = JsonKey::Text(key.to_owned());
        match entries.iter_mut().find(|(existing, _)| *existing == key) {
            Some((_, existing)) => *existing = value,
            None => entries.push((key, value)),
        }
    }
    JsonValue::Object(entries)
}

const ROLE_QUERY: &[QueryParam] = &[
                QueryParam {
                    name: "project",
                    scalar: Scalar::Text { min_length: Some(1) },
                    required: true,
                    many: false,
                },
                QueryParam::optional("includeRevoked", Scalar::Boolean),
            ];
const PLAN_QUERY: &[QueryParam] = &[
                QueryParam {
                    name: "select",
                    scalar: TEXT,
                    required: false,
                    many: true,
                },
                QueryParam::optional("start", TEXT),
                QueryParam::optional("deployment", TEXT),
                QueryParam::optional("counts", Scalar::Boolean),
                QueryParam::optional("changed", Scalar::Boolean),
                QueryParam::optional("include_missing_upstream", Scalar::Boolean),
            ];
const DIFF_QUERY: &[QueryParam] = &[QueryParam::optional("against", TEXT)];
const TICKS_QUERY: &[QueryParam] = &[
                QueryParam::optional("limit", Scalar::Integer { ge: Some(1), le: Some(500) }),
                QueryParam::optional("after", TEXT),
                QueryParam::optional("before", TEXT),
            ];
const EVENTS_QUERY: &[QueryParam] = &[QueryParam::optional("after", NON_NEGATIVE)];
const FEED_QUERY: &[QueryParam] = &[QueryParam::optional("after", NON_NEGATIVE)];

/// The route table, in the order the earlier application registered its routes.
pub(crate) fn routes() -> Vec<Route<Endpoint>> {
    const GET: &[&str] = &["GET"];
    const POST: &[&str] = &["POST"];
    const PATCH: &[&str] = &["PATCH"];
    const DELETE: &[&str] = &["DELETE"];
    const UUID: Scalar = Scalar::Uuid;
    let model = |module: &'static str, name: &'static str| BodySpec::Model(module, name);
    vec![
        // Authentication and account administration.
        Route::new(GET, "/api/auth/config", Endpoint::new(auth::read_auth_config)),
        Route::new(GET, "/api/auth/me", Endpoint::new(auth::read_current_user)),
        Route::new(POST, "/api/auth/login", Endpoint::new(auth::login).body(model(AUTH_MODELS, "LoginRequest"))),
        Route::new(POST, "/api/auth/logout", Endpoint::new(auth::logout)),
        Route::new(GET, "/api/admin/users", Endpoint::new(auth::list_users)),
        Route::new(POST, "/api/admin/users", Endpoint::new(auth::create_user).body(model(AUTH_MODELS, "CreateUserRequest"))),
        Route::new(
            PATCH,
            "/api/admin/users/{user_id}",
            Endpoint::new(auth::update_user).path(&[UUID]).body(model(AUTH_MODELS, "UpdateUserRequest")),
        ),
        Route::new(
            POST,
            "/api/admin/users/{user_id}/password",
            Endpoint::new(auth::reset_password).path(&[UUID]).body(model(AUTH_MODELS, "PasswordResetRequest")),
        ),
        Route::new(
            POST,
            "/api/admin/users/{user_id}/roles",
            Endpoint::new(auth::grant_role).path(&[UUID]).body(model(AUTH_MODELS, "RoleRequest")),
        ),
        Route::new(
            DELETE,
            "/api/admin/users/{user_id}/roles/{role_name}",
            Endpoint::new(auth::revoke_role).path(&[UUID, TEXT]),
        ),
        Route::new(
            GET,
            "/api/admin/users/{user_id}/project-roles",
            Endpoint::new(auth::list_project_roles).path(&[UUID]).query(ROLE_QUERY),
        ),
        Route::new(
            POST,
            "/api/admin/users/{user_id}/project-roles",
            Endpoint::new(auth::grant_project_role).path(&[UUID]).body(model(AUTH_MODELS, "ProjectRoleRequest")),
        ),
        Route::new(
            DELETE,
            "/api/admin/project-roles/{assignment_id}",
            Endpoint::new(auth::revoke_project_role).path(&[UUID]),
        ),
        Route::new(GET, "/api/admin/audit", Endpoint::new(auth::read_account_audit)),
        // Status, state and project.
        Route::new(GET, "/api/status", Endpoint::new(read_status)),
        Route::new(POST, "/api/warehouse/refresh", Endpoint::new(refresh_warehouse)),
        Route::new(GET, "/api/state", Endpoint::new(read_state)),
        Route::new(
            GET,
            "/api/plan",
            Endpoint::new(read_plan).query(PLAN_QUERY),
        ),
        Route::new(POST, "/api/reload", Endpoint::new(reload_project)),
        Route::new(GET, "/api/auth/capabilities", Endpoint::new(read_capabilities)),
        Route::new(GET, "/api/access-policy", Endpoint::new(read_access_policy)),
        Route::new(GET, "/api/definitions", Endpoint::new(read_definitions)),
        Route::new(GET, "/api/topics", Endpoint::new(read_topics)),
        // Deployments.
        Route::new(GET, "/api/deployments", Endpoint::new(read_deployments)),
        Route::new(GET, "/api/deployments/{deployment_id}", Endpoint::new(read_one_deployment).path(&[TEXT])),
        Route::new(
            GET,
            "/api/deployments/{deployment_id}/diff",
            Endpoint::new(read_deployment_diff).path(&[TEXT]).query(DIFF_QUERY),
        ),
        Route::new(POST, "/api/deployments/{deployment_id}/promote", Endpoint::new(promote_deployment).path(&[TEXT])),
        Route::new(
            POST,
            "/api/deployments/cleanup",
            Endpoint::new(cleanup_deployments).body(model(DEV_MODELS, "DeploymentCleanupRequest")),
        ),
        // Destruction.
        Route::new(GET, "/api/destruction/pipelines/inactive", Endpoint::new(destruction::read_inactive_pipelines)),
        Route::new(
            POST,
            "/api/destruction/plans",
            Endpoint::new(destruction::create_plan).body(model(DEV_MODELS, "DestructionPlanRequest")),
        ),
        Route::new(GET, "/api/destruction/plans/{plan_id}", Endpoint::new(destruction::read_plan).path(&[TEXT])),
        Route::new(POST, "/api/destruction/plans/{plan_id}/review", Endpoint::new(destruction::review_plan).path(&[TEXT])),
        Route::new(
            POST,
            "/api/runs/{invocation_id}/recovery-plan",
            Endpoint::new(destruction::create_recovery_plan).path(&[TEXT]),
        ),
        Route::new(
            POST,
            "/api/destruction/plans/{plan_id}/execute",
            Endpoint::new(destruction::execute_plan)
                .path(&[TEXT])
                .body(model(DEV_MODELS, "DestructionExecutionRequest"))
                .status(202),
        ),
        // Source messages.
        Route::new(
            POST,
            "/api/sources/{name}/messages",
            Endpoint::new(read_messages).path(&[TEXT]).body(model(DEV_MODELS, "MessagesQueryRequest")),
        ),
        Route::new(
            POST,
            "/api/sources/{name}/messages/record",
            Endpoint::new(read_message_record).path(&[TEXT]).body(model(DEV_MODELS, "MessageRecordRequest")),
        ),
        Route::new(
            POST,
            "/api/sources/{name}/messages/facets",
            Endpoint::new(read_message_facets).path(&[TEXT]).body(model(DEV_MODELS, "MessageFacetsRequest")),
        ),
        // Sensors.
        Route::new(GET, "/api/sensors", Endpoint::new(sensors::read_sensors)),
        Route::new(GET, "/api/sensors/dead-letters", Endpoint::new(sensors::read_dead_letters)),
        Route::new(
            GET,
            "/api/sensors/{sensor_name}/ticks",
            Endpoint::new(sensors::read_sensor_ticks).path(&[TEXT]).query(TICKS_QUERY),
        ),
        Route::new(
            POST,
            "/api/sensors/{sensor_name}/status",
            Endpoint::new(sensors::set_sensor_status).path(&[TEXT]).body(model(DEV_MODELS, "SensorStatusRequest")),
        ),
        Route::new(
            POST,
            "/api/sensors/dead-letters/retry",
            Endpoint::new(sensors::retry_dead_letter).body(model(DEV_MODELS, "SensorDeadLetterActionRequest")),
        ),
        Route::new(
            POST,
            "/api/sensors/dead-letters/skip",
            Endpoint::new(sensors::skip_dead_letter).body(model(DEV_MODELS, "SensorDeadLetterActionRequest")),
        ),
        // Checks, audits and run history.
        Route::new(POST, "/api/checks/run", Endpoint::new(run_check).body(model(DEV_MODELS, "ChecksRunRequest"))),
        Route::new(POST, "/api/audits/run", Endpoint::new(run_audits).body(model(DEV_MODELS, "AuditBatchRunRequest"))),
        Route::new(GET, "/api/checks/status", Endpoint::new(read_checks_status)),
        Route::new(GET, "/api/audit-scheduler", Endpoint::new(read_audit_scheduler)),
        Route::new(GET, "/api/runs", Endpoint::new(read_run_history)),
        Route::new(
            GET,
            "/api/runs/{invocation_id}/events",
            Endpoint::new(read_one_run_events).path(&[TEXT]).query(EVENTS_QUERY),
        ),
        Route::new(
            GET,
            "/api/runs/{invocation_id}/statements/{statement_sequence}",
            Endpoint::new(read_one_run_statement).path(&[TEXT, INTEGER]),
        ),
        // Builds.
        Route::new(POST, "/api/build", Endpoint::new(start_build).body(model(DEV_MODELS, "BuildRunRequest"))),
        Route::new(
            GET,
            "/api/build/current",
            Endpoint::new(read_build_feed).query(FEED_QUERY),
        ),
        Route::new(POST, "/api/build/cancel", Endpoint::new(cancel_build).body(BodySpec::StringMapping)),
        Route::new(POST, "/api/build/kill", Endpoint::new(kill_build).body(BodySpec::StringMapping)),
        Route::new(GET, "/api/bootstrap", Endpoint::new(read_bootstrap)),
    ]
}
