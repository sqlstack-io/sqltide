//! The frozen-plan API for recorded pipeline destruction and target reset.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyTuple};
use sqltide_json::JsonValue;

use super::app::{Call, DevApp};
use super::auth::{authenticated, user_id};
use super::authz;
use super::http::{Failure, HttpReply, HttpRequest, Outcome, object, py_json, text};
use super::routes::{catching, database, invoke, locked, merged, required_connection, servable, state, warehouse, within, VALUE_ERROR};
use super::support::{ADAPTER_ERROR, attr, is_instance, message, translate};
use crate::handling::while_handling;
use crate::py_kwargs;

const EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const MODELS: &str = "sqltide.executor.destruction.models";
const NATIVE: &str = "sqltide._native";
const LOGGER_NAME: &str = "sqltide.dev_server._helpers.server.destruction_routes";

const PLAN_LOOKUP_ERRORS: [(&str, &str, u16); 3] = [
    (EXCEPTIONS, "DestructionPlanExpiredError", 410),
    (EXCEPTIONS, "DestructionPlanNotFoundError", 404),
    (EXCEPTIONS, "DestructionPlanCorruptError", 502),
];

fn store<'py>(call: &Call<'py, '_>) -> PyResult<Bound<'py, PyAny>> {
    call.service(|services| &services.control_stores).getattr("destruction_plans")
}

fn actor_id(call: &Call<'_, '_>) -> Outcome<String> {
    Ok(user_id(&authenticated(call)?)?.str()?.to_string())
}

/// The compiled target name, else the selected target, else empty.
fn current_target(call: &Call<'_, '_>, analysis: &Bound<'_, PyAny>) -> PyResult<String> {
    let compiled = analysis.getattr("compiled_project")?.getattr("target_name")?;
    if compiled.is_truthy()? {
        return compiled.extract();
    }
    Ok(call
        .app
        .services
        .selected_target
        .clone()
        .filter(|target| !target.is_empty())
        .unwrap_or_default())
}

/// The shared client, or 503 without a connection or database.
fn connection<'py>(call: &Call<'py, '_>) -> Outcome<Bound<'py, PyAny>> {
    required_connection(call)
}

fn new_error(py: Python<'_>, name: &str, detail: &str) -> PyErr {
    match attr(py, EXCEPTIONS, name).and_then(|class| class.call1((detail,))) {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

fn names(value: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    value.try_iter()?.map(|item| item?.extract::<String>()).collect()
}

fn destruction_request<'py>(
    call: &Call<'py, '_>,
    operation: &Bound<'py, PyAny>,
    target: &str,
    pipeline_names: &[String],
    included: &[String],
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    invoke(
        py,
        MODELS,
        "DestructionRequest",
        py_kwargs!(py;
            operation = operation,
            target = target,
            database = database(call),
            metadata_database = database(call),
            pipeline_names = PyTuple::new(py, pipeline_names)?,
            included_dependent_pipeline_names = PyTuple::new(py, included)?,
        )?,
    )
}

fn plan_destruction<'py>(
    py: Python<'py>,
    request: &Bound<'py, PyAny>,
    analysis: &Bound<'py, PyAny>,
    connection: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    invoke(py, NATIVE, "plan_destruction", py_kwargs!(py; request = request, analysis = analysis, connection = connection)?)
}

fn isoformat(value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    py_json(&value.call_method0("isoformat")?)
}

/// The plan as the browser reviews it.
fn plan_payload(plan: &Bound<'_, PyAny>, reviewed_at: Option<&Bound<'_, PyAny>>) -> PyResult<JsonValue> {
    let mut resources: Vec<JsonValue> = Vec::new();
    for relation in plan.getattr("relations")?.try_iter()? {
        let relation = relation?;
        let logical_names = relation.getattr("logical_names")?;
        let pipeline_names = relation.getattr("pipeline_names")?;
        resources.push(object(vec![
            ("name", py_json(&relation.getattr("name")?)?),
            ("kind", text(relation.getattr("kind")?.str()?.to_string())),
            ("logicalName", py_json(&logical_names.get_item(0)?)?),
            ("logicalNames", py_json(&logical_names)?),
            (
                "pipelineName",
                if pipeline_names.is_truthy()? { py_json(&pipeline_names.get_item(0)?)? } else { JsonValue::Null },
            ),
            ("exists", py_json(&relation.getattr("exists")?)?),
            ("bytes", py_json(&relation.getattr("total_bytes")?)?),
            ("activeParts", py_json(&relation.getattr("active_parts")?)?),
        ]));
    }
    Ok(object(vec![
        ("planId", py_json(&plan.getattr("plan_id")?)?),
        ("planFingerprint", py_json(&plan.getattr("plan_fingerprint")?)?),
        ("operation", py_json(&plan.getattr("operation")?.getattr("value")?)?),
        ("target", py_json(&plan.getattr("target")?)?),
        ("database", py_json(&plan.getattr("database")?)?),
        ("selectedPipelines", py_json(&plan.getattr("requested_pipeline_names")?)?),
        ("includedDependentPipelines", py_json(&plan.getattr("included_dependent_pipeline_names")?)?),
        ("affectedPipelines", py_json(&plan.getattr("affected_pipeline_names")?)?),
        ("requiredDependentPipelines", JsonValue::Array(Vec::new())),
        ("blocked", JsonValue::Bool(false)),
        ("models", py_json(&plan.getattr("affected_model_names")?)?),
        ("resources", JsonValue::Array(resources)),
        ("managedSourcesIncluded", JsonValue::Bool(!plan.getattr("preserves_sources")?.is_truthy()?)),
        ("retainedReplayDataIncluded", JsonValue::Bool(!plan.getattr("preserves_replay_data")?.is_truthy()?)),
        ("estimatedBytes", py_json(&plan.getattr("estimated_bytes")?)?),
        ("dropSizeLimitBytes", py_json(&plan.getattr("relation_drop_size_limit")?)?),
        ("dropSizeServerLimitBytes", py_json(&plan.getattr("relation_drop_size_server_limit")?)?),
        ("dropSizeOverrideBytes", py_json(&plan.getattr("relation_drop_size_override")?)?),
        ("dropSizePolicyObserved", py_json(&plan.getattr("relation_drop_size_policy_observed")?)?),
        ("challengeValues", py_json(&plan.getattr("challenges")?)?),
        ("expiresAt", isoformat(&plan.getattr("expires_at")?)?),
        ("reviewedAt", match reviewed_at {
            Some(reviewed_at) => isoformat(reviewed_at)?,
            None => JsonValue::Null,
        }),
    ]))
}

fn conflict_detail(py: Python<'_>, error: &PyErr, extra: (&str, JsonValue)) -> PyResult<Failure> {
    Ok(Failure::detail(409, object(vec![("message", text(message(py, error)?)), extra])))
}

pub(crate) fn create_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor: String = actor_id(call)?;
    let analysis = servable(call)?;
    let target: String = current_target(call, &analysis)?;
    let client = connection(call)?;
    let request = call.body()?;
    let operation = request.getattr("operation")?;
    let operation_text: String = operation.extract()?;
    let requested: Vec<String> = names(&request.getattr("pipelineNames")?)?;
    let included: Vec<String> = names(&request.getattr("includedDependentPipelineNames")?)?;
    authz::require_destruction(call, &analysis, &operation_text, requested.clone())?;
    let attempt: Outcome<JsonValue> = (|| {
        let plan = locked(call, || {
            let request = destruction_request(call, &operation, &target, &requested, &included)?;
            Ok(plan_destruction(py, &request, &analysis, &client)?)
        })?;
        authz::require_destruction(call, &analysis, &operation_text, names(&plan.getattr("affected_pipeline_names")?)?)?;
        store(call)?.call_method("save", (), Some(&py_kwargs!(py; plan = &plan, actor = actor.as_str())?))?;
        Ok(plan_payload(&plan, None)?)
    })();
    let error: PyErr = match attempt {
        Ok(payload) => return call.payload_json(&payload),
        Err(Failure::Python(error)) => error,
        Err(other) => return Err(other),
    };
    if is_instance(py, &error, EXCEPTIONS, "DestructionDependencyError")? {
        let dependents: Vec<String> = names(&error.value(py).getattr("dependent_pipeline_names")?)?;
        let handled: Outcome<JsonValue> = (|| {
            let mut widened: std::collections::BTreeSet<String> = included.iter().cloned().collect();
            widened.extend(dependents.iter().cloned());
            let widened: Vec<String> = widened.into_iter().collect();
            let blocked = locked(call, || {
                let request = destruction_request(call, &operation, &target, &requested, &widened)?;
                Ok(plan_destruction(py, &request, &analysis, &client)?)
            })?;
            authz::require_destruction(call, &analysis, &operation_text, names(&blocked.getattr("affected_pipeline_names")?)?)?;
            Ok(merged(
                plan_payload(&blocked, None)?,
                vec![
                    ("selectedPipelines", JsonValue::Array(requested.iter().cloned().map(JsonValue::String).collect())),
                    ("requiredDependentPipelines", JsonValue::Array(dependents.iter().cloned().map(JsonValue::String).collect())),
                    ("blocked", JsonValue::Bool(true)),
                ],
            ))
        })();
        return match handled {
            Ok(payload) => call.payload_json(&payload),
            Err(Failure::Python(raised)) => Err(Failure::Python(while_handling(py, raised, &error))),
            Err(other) => Err(other),
        };
    }
    if is_instance(py, &error, EXCEPTIONS, "DestructionExternalDependencyError")? {
        let relations = py_json(&pyo3::types::PyList::new(py, names(&error.value(py).getattr("relation_names")?)?)?.into_any())?;
        return Err(conflict_detail(py, &error, ("blockingRelations", relations))?);
    }
    if is_instance(py, &error, EXCEPTIONS, "DestructionResourceError")? {
        return Err(conflict_detail(py, &error, ("reason", text("resource_conflict")))?);
    }
    Err(translate(py, error, &[(EXCEPTIONS, "DestructionSelectionError", 400), VALUE_ERROR, ADAPTER_ERROR]))
}

pub(crate) fn read_inactive_pipelines(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let analysis = servable(call)?;
    let target: String = current_target(call, &analysis)?;
    let operation = attr(py, "sqltide.executor.destruction.types", "DestructionOperation")?.getattr("DESTROY_PIPELINES")?;
    let request = destruction_request(call, &operation, &target, &[], &[])?;
    authz::require_destruction(call, &analysis, "destroy_pipelines", Vec::new())?;
    let client = connection(call)?;
    let attempt: Outcome<JsonValue> = (|| {
        let pipelines = locked(call, || {
            Ok(invoke(
                py,
                NATIVE,
                "list_inactive_pipelines",
                py_kwargs!(py; request = &request, analysis = &analysis, connection = &client)?,
            )?)
        })?;
        let mut listed: Vec<Bound<'_, PyAny>> = Vec::new();
        for pipeline in pipelines.try_iter()? {
            listed.push(pipeline?);
        }
        let pipeline_names: Vec<String> = listed
            .iter()
            .map(|pipeline| pipeline.getattr("name")?.extract())
            .collect::<PyResult<_>>()?;
        authz::require_destruction(call, &analysis, "destroy_pipelines", pipeline_names)?;
        let mut items: Vec<JsonValue> = Vec::new();
        for pipeline in &listed {
            items.push(object(vec![
                ("name", py_json(&pipeline.getattr("name")?)?),
                ("modelCount", py_json(&pipeline.getattr("model_count")?)?),
                ("resourceCount", py_json(&pipeline.getattr("resource_count")?)?),
                ("lastPublishedAt", py_json(&pipeline.getattr("last_published_at")?)?),
            ]));
        }
        Ok(object(vec![("pipelines", JsonValue::Array(items))]))
    })();
    match attempt {
        Ok(payload) => call.payload_json(&payload),
        Err(Failure::Python(error)) => {
            if is_instance(py, &error, EXCEPTIONS, "DestructionResourceError")? {
                return Err(conflict_detail(py, &error, ("reason", text("resource_conflict")))?);
            }
            Err(translate(py, error, &[(EXCEPTIONS, "DestructionSelectionError", 400), VALUE_ERROR, ADAPTER_ERROR]))
        }
        Err(other) => Err(other),
    }
}

pub(crate) fn read_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor: String = actor_id(call)?;
    let plan_id: &str = call.path[0].text();
    let lookup: Outcome<(Bound<'_, PyAny>, Option<Bound<'_, PyAny>>)> = (|| {
        let store = store(call)?;
        let plan = store.call_method("get", (), Some(&py_kwargs!(py; plan_id = plan_id, actor = actor.as_str())?))?;
        let reviewed_at = match store.call_method("reviewed_at", (), Some(&py_kwargs!(py; plan_id = plan_id, actor = actor.as_str())?)) {
            Ok(reviewed_at) => Some(reviewed_at),
            Err(error) if is_instance(py, &error, EXCEPTIONS, "DestructionPlanNotReviewedError")? => None,
            Err(error) => return Err(error.into()),
        };
        Ok((plan, reviewed_at))
    })();
    let (plan, reviewed_at) = catching(py, lookup, &PLAN_LOOKUP_ERRORS)?;
    let analysis = servable(call)?;
    authz::require_destruction(
        call,
        &analysis,
        &plan.getattr("operation")?.getattr("value")?.extract::<String>()?,
        names(&plan.getattr("affected_pipeline_names")?)?,
    )?;
    call.payload_json(&plan_payload(&plan, reviewed_at.as_ref())?)
}

/// Refuse a plan that predates the frozen DROP evidence or targets another database, then
/// authorize its exact scope.
fn authorize_plan(call: &Call<'_, '_>, plan: &Bound<'_, PyAny>, analysis: &Bound<'_, PyAny>) -> Outcome<()> {
    let py: Python<'_> = call.py;
    if !plan.getattr("relation_drop_size_policy_observed")?.is_truthy()? {
        return Err(new_error(py, "DestructionDriftError", "Destruction plan predates frozen DROP safety evidence; create a fresh plan").into());
    }
    let target: String = current_target(call, analysis)?;
    let same: bool = plan.getattr("target")?.eq(target.as_str())? && plan.getattr("database")?.eq(database(call))?;
    if !same {
        return Err(new_error(
            py,
            "DestructionDriftError",
            "Destruction plan target or physical database differs from the active server target",
        )
        .into());
    }
    authz::require_destruction(
        call,
        analysis,
        &plan.getattr("operation")?.getattr("value")?.extract::<String>()?,
        names(&plan.getattr("affected_pipeline_names")?)?,
    )
}

pub(crate) fn review_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor: String = actor_id(call)?;
    let plan_id: &str = call.path[0].text();
    let attempt: Outcome<(Bound<'_, PyAny>, Bound<'_, PyAny>)> = (|| {
        let store = store(call)?;
        let plan = store.call_method("get", (), Some(&py_kwargs!(py; plan_id = plan_id, actor = actor.as_str())?))?;
        let analysis = servable(call)?;
        authorize_plan(call, &plan, &analysis)?;
        let reviewed_at = store.call_method("mark_reviewed", (), Some(&py_kwargs!(py; plan_id = plan_id, actor = actor.as_str())?))?;
        Ok((plan, reviewed_at))
    })();
    let mut clauses: Vec<(&str, &str, u16)> = PLAN_LOOKUP_ERRORS.to_vec();
    clauses.push((EXCEPTIONS, "DestructionDriftError", 409));
    let (plan, reviewed_at) = catching(py, attempt, &clauses)?;
    call.payload_json(&plan_payload(&plan, Some(&reviewed_at))?)
}

/// The recorded request of a terminal failed destruction run on this server's target.
fn recovery_request<'py>(call: &Call<'py, '_>, run: &Bound<'py, PyAny>, analysis: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    let recovery = |detail: &str| new_error(py, "DestructionRecoveryError", detail);
    if !run.call_method1("get", ("mode",))?.eq("destructive")? || !run.call_method1("get", ("outcome",))?.eq("failed")? {
        return Err(recovery("Only a terminal failed destruction run can be recovered"));
    }
    let expected_project = invoke(
        py,
        "sqltide.executor.observability.main.logical_project_identity",
        "logical_project_identity",
        py_kwargs!(py; project_dir = call.service(|services| &services.project_dir))?,
    )?;
    if !run.call_method1("get", ("projectIdentity",))?.eq(&expected_project)? {
        return Err(new_error(py, "DestructionRecoveryNotFoundError", "Failed destruction run was not found"));
    }
    let summary = run.call_method1("get", ("summary",))?;
    if !summary.is_instance(&attr(py, "collections.abc", "Mapping")?)? {
        return Err(recovery("Failed destruction run has no complete recovery evidence"));
    }
    let kind = summary.call_method1("get", ("operationKind",))?;
    if !kind.is_instance_of::<pyo3::types::PyString>() {
        return Err(recovery("Failed destruction run has no recorded operation kind"));
    }
    let operation = match attr(py, "sqltide.executor.destruction.types", "DestructionOperation")?.call1((&kind,)) {
        Ok(operation) => operation,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
            let raised = recovery("Failed destruction run operation kind is invalid");
            let value = raised.value(py);
            value.setattr("__cause__", error.value(py))?;
            value.setattr("__context__", error.value(py))?;
            return Err(raised);
        }
        Err(error) => return Err(error),
    };
    let destroy: bool = operation.getattr("value")?.extract::<String>()? == "destroy_pipelines";
    let expected_command: &str = if destroy { "destroy pipelines" } else { "reset target" };
    if !run.call_method1("get", ("command",))?.eq(expected_command)? {
        return Err(recovery("Failed destruction run command and evidence disagree"));
    }
    let target: String = current_target(call, analysis)?;
    if !summary.call_method1("get", ("target",))?.eq(target.as_str())? || !summary.call_method1("get", ("database",))?.eq(database(call))? {
        return Err(recovery("Failed destruction run target or database differs from the active server"));
    }
    let recorded = |field: &str| -> PyResult<Vec<String>> {
        let value = summary.call_method1("get", (field,))?;
        let invalid = || recovery(&format!("Failed destruction run has invalid recorded {field} evidence"));
        if !value.is_instance_of::<pyo3::types::PyList>() {
            return Err(invalid());
        }
        let mut items: Vec<String> = Vec::new();
        for item in value.try_iter()? {
            let item = item?;
            let Ok(text) = item.cast::<pyo3::types::PyString>() else {
                return Err(invalid());
            };
            if text.is_empty()? {
                return Err(invalid());
            }
            items.push(text.to_str()?.to_owned());
        }
        Ok(items)
    };
    let selection: Vec<String> = recorded("originalSelection")?;
    let dependents: Vec<String> = recorded("includedDependentPipelines")?;
    destruction_request(call, &operation, &target, &selection, &dependents)
}

pub(crate) fn create_recovery_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let actor: String = actor_id(call)?;
    let analysis = servable(call)?;
    let client = connection(call)?;
    let invocation_id: &str = call.path[0].text();
    let attempt: Outcome<JsonValue> = (|| {
        let manager = warehouse(call).call_method0("read_connection")?;
        let run = within(&manager, |read_connection| {
            if read_connection.is_none() || call.app.services.database.is_none() {
                return Err(Failure::status(503, "no warehouse read connection"));
            }
            Ok(invoke(
                py,
                "sqltide.dev_server._helpers.queries.runs_query",
                "read_destruction_recovery_run",
                py_kwargs!(py; connection = read_connection, database = database(call), invocation_id = invocation_id)?,
            )?)
        })?;
        if run.is_none() {
            return Err(Failure::status(404, "failed run was not found"));
        }
        let request = recovery_request(call, &run, &analysis)?;
        let operation_text: String = request.getattr("operation")?.str()?.to_string();
        let mut scope: std::collections::BTreeSet<String> = names(&request.getattr("pipeline_names")?)?.into_iter().collect();
        scope.extend(names(&request.getattr("included_dependent_pipeline_names")?)?);
        authz::require_destruction(call, &analysis, &operation_text, scope.into_iter().collect())?;
        let plan = locked(call, || Ok(plan_destruction(py, &request, &analysis, &client)?))?;
        authz::require_destruction(call, &analysis, &operation_text, names(&plan.getattr("affected_pipeline_names")?)?)?;
        store(call)?.call_method("save", (), Some(&py_kwargs!(py; plan = &plan, actor = actor.as_str())?))?;
        Ok(plan_payload(&plan, None)?)
    })();
    match attempt {
        Ok(payload) => call.payload_json(&payload),
        Err(Failure::Python(error)) => {
            if is_instance(py, &error, EXCEPTIONS, "DestructionDependencyError")? {
                let missing = py_json(&pyo3::types::PyList::new(py, names(&error.value(py).getattr("dependent_pipeline_names")?)?)?.into_any())?;
                return Err(conflict_detail(py, &error, ("missingPipelines", missing))?);
            }
            Err(translate(
                py,
                error,
                &[
                    (EXCEPTIONS, "DestructionExternalDependencyError", 409),
                    (EXCEPTIONS, "DestructionResourceError", 409),
                    (EXCEPTIONS, "DestructionRecoveryNotFoundError", 404),
                    (EXCEPTIONS, "DestructionRecoveryError", 409),
                    (EXCEPTIONS, "DestructionSelectionError", 400),
                    VALUE_ERROR,
                    ADAPTER_ERROR,
                ],
            ))
        }
        Err(other) => Err(other),
    }
}

/// A route failure as the Python exception a background caller sees.
fn into_python(py: Python<'_>, failure: Failure) -> PyErr {
    match failure {
        Failure::Python(error) => error,
        Failure::Http { status, detail, .. } => {
            let detail: String = match detail {
                JsonValue::String(detail) => detail,
                other => super::http::stdlib_json(&other).unwrap_or_default(),
            };
            pyo3::exceptions::PyPermissionError::new_err(format!("{status}: {detail}"))
        }
        Failure::Validation(_) => pyo3::exceptions::PyValueError::new_err("invalid request"),
    }
    .clone_ref(py)
}

/// Recompile, re-authorize the frozen scope and plan again, for drift detection before the
/// first destructive statement.
fn fresh_plan<'py>(call: &Call<'py, '_>, plan: &Bound<'py, PyAny>, client: &Bound<'py, PyAny>) -> Outcome<Bound<'py, PyAny>> {
    let py: Python<'py> = call.py;
    let outcome = state(call).call_method0("reload")?;
    let analysis = outcome.getattr("analysis")?;
    if analysis.is_none() {
        return Err(new_error(py, "DestructionDriftError", "Project compilation changed or failed after plan review").into());
    }
    authorize_plan(call, plan, &analysis)?;
    let request = invoke(
        py,
        MODELS,
        "DestructionRequest",
        py_kwargs!(py;
            operation = plan.getattr("operation")?,
            target = plan.getattr("target")?,
            database = plan.getattr("database")?,
            metadata_database = plan.getattr("metadata_database")?,
            pipeline_names = plan.getattr("requested_pipeline_names")?,
            included_dependent_pipeline_names = plan.getattr("included_dependent_pipeline_names")?,
        )?,
    )?;
    Ok(plan_destruction(py, &request, &analysis, client)?)
}

/// What the background execution needs once the request has returned.
struct Dispatch {
    app: Arc<DevApp>,
    request: HttpRequest,
    authenticated: Py<PyAny>,
    plan: Py<PyAny>,
    plan_id: String,
    actor_id: String,
    actor_name: Py<PyAny>,
    challenges: Py<PyAny>,
    reviewed_at: Py<PyAny>,
    connection: Py<PyAny>,
    observation_connection: Py<PyAny>,
    invocation_id: String,
}

impl Dispatch {
    fn run(&self, py: Python<'_>) {
        let call: Call<'_, '_> = Call::detached(py, &self.app, &self.request, Some(self.authenticated.bind(py).clone()));
        let outcome: Outcome<()> = (|| {
            locked(&call, || {
                let replan = self.replan(py)?;
                let actor = invoke(
                    py,
                    MODELS,
                    "DestructionActor",
                    py_kwargs!(py; actor_id = self.actor_id.as_str(), actor_name = self.actor_name.bind(py))?,
                )?;
                invoke(
                    py,
                    NATIVE,
                    "execute_destruction",
                    py_kwargs!(py;
                        frozen_plan = self.plan.bind(py),
                        actor = actor,
                        challenge_responses = self.challenges.bind(py),
                        reviewed_at = self.reviewed_at.bind(py),
                        store = store(&call)?,
                        connection = self.connection.bind(py),
                        observation_connection = self.observation_connection.bind(py),
                        project_dir = call.service(|services| &services.project_dir),
                        replan = replan,
                        invocation_id = self.invocation_id.as_str(),
                    )?,
                )?;
                Ok(())
            })?;
            state(&call).getattr("snapshot")?.call_method0("invalidate")?;
            Ok(())
        })();
        if let Err(failure) = outcome {
            let error: PyErr = into_python(py, failure);
            let logged: PyResult<()> = (|| {
                let logger = attr(py, "logging", "getLogger")?.call1((LOGGER_NAME,))?;
                logger.call_method(
                    "error",
                    ("Background destruction execution failed for invocation %s", self.invocation_id.as_str()),
                    Some(&py_kwargs!(py; exc_info = error.value(py))?),
                )?;
                Ok(())
            })();
            if let Err(logging_error) = logged {
                logging_error.print(py);
            }
        }
        if let Ok(mut dispatches) = self.app.dispatches.lock() {
            dispatches.remove(&self.plan_id);
        }
    }

    /// The callback execution uses to plan again against a fresh compile.
    fn replan<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCFunction>> {
        let app: Arc<DevApp> = Arc::clone(&self.app);
        let request: HttpRequest = self.request.clone();
        let authenticated: Py<PyAny> = self.authenticated.clone_ref(py);
        let plan: Py<PyAny> = self.plan.clone_ref(py);
        let connection: Py<PyAny> = self.connection.clone_ref(py);
        PyCFunction::new_closure(py, None, None, move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| -> PyResult<Py<PyAny>> {
            let py: Python<'_> = args.py();
            let call: Call<'_, '_> = Call::detached(py, &app, &request, Some(authenticated.bind(py).clone()));
            fresh_plan(&call, plan.bind(py), connection.bind(py))
                .map(Bound::unbind)
                .map_err(|failure| into_python(py, failure))
        })
    }
}

pub(crate) fn execute_plan(call: &Call<'_, '_>) -> Outcome<HttpReply> {
    let py: Python<'_> = call.py;
    let authenticated = authenticated(call)?;
    let actor: String = actor_id(call)?;
    let plan_id: String = call.path[0].text().to_owned();
    let client = connection(call)?;
    let attempt: Outcome<String> = (|| {
        let store = store(call)?;
        let plan = store.call_method("get", (), Some(&py_kwargs!(py; plan_id = plan_id.as_str(), actor = actor.as_str())?))?;
        let analysis = servable(call)?;
        authorize_plan(call, &plan, &analysis)?;
        let reviewed_at = store.call_method("reviewed_at", (), Some(&py_kwargs!(py; plan_id = plan_id.as_str(), actor = actor.as_str())?))?;
        let responses = PyTuple::new(py, call.body()?.getattr("responses")?.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
        if !responses.as_any().eq(plan.getattr("challenges")?)? {
            return Err(new_error(py, "DestructionChallengeError", "Challenge responses must exactly match the frozen plan in order").into());
        }
        let observation_connection = warehouse(call).getattr("observation_connection")?;
        if observation_connection.is_none() {
            return Err(Failure::status(503, "no warehouse observation connection"));
        }
        let reserved: bool = call
            .app
            .dispatches
            .lock()
            .is_ok_and(|mut dispatches| dispatches.insert(plan_id.clone()));
        if !reserved {
            let quoted: String = pyo3::types::PyString::new(py, &plan_id).repr()?.to_string();
            return Err(new_error(py, "DestructionPlanNotFoundError", &format!("Destruction plan {quoted} is already executing")).into());
        }
        let invocation_id: String = attr(py, "uuid", "uuid4")?.call0()?.str()?.to_string();
        let dispatch: Dispatch = Dispatch {
            app: Arc::clone(call.app),
            request: call.request.clone(),
            authenticated: authenticated.clone().unbind(),
            plan: plan.clone().unbind(),
            plan_id: plan_id.clone(),
            actor_id: actor.clone(),
            actor_name: authenticated.getattr("principal")?.getattr("username")?.unbind(),
            challenges: responses.into_any().unbind(),
            reviewed_at: reviewed_at.unbind(),
            connection: client.clone().unbind(),
            observation_connection: observation_connection.unbind(),
            invocation_id: invocation_id.clone(),
        };
        let target = PyCFunction::new_closure(py, None, None, move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| {
            dispatch.run(args.py());
        })?;
        attr(py, "threading", "Thread")?
            .call((), Some(&py_kwargs!(py; target = target, name = format!("sqltide-destruction-{invocation_id}"), daemon = true)?))?
            .call_method0("start")?;
        Ok(invocation_id)
    })();
    let clauses: [(&str, &str, u16); 9] = [
        PLAN_LOOKUP_ERRORS[0],
        PLAN_LOOKUP_ERRORS[1],
        PLAN_LOOKUP_ERRORS[2],
        (EXCEPTIONS, "DestructionChallengeError", 400),
        (EXCEPTIONS, "DestructionPlanNotReviewedError", 409),
        (EXCEPTIONS, "DestructionDriftError", 409),
        ("sqltide.adapter.exceptions", "AdapterTargetMutationLockError", 409),
        ADAPTER_ERROR,
        (EXCEPTIONS, "DestructionRecordingError", 502),
    ];
    let invocation_id: String = catching(py, attempt, &clauses)?;
    call.payload_json(&object(vec![("invocationId", text(invocation_id)), ("status", text("starting"))]))
}
