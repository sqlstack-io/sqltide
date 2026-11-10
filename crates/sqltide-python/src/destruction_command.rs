//! `sqt destroy`: authorize the operating-system user as an active built-in administrator in the
//! control store, plan the destruction, render it for mandatory review and typed challenges, then
//! execute the frozen plan with a replan callback that reauthorizes the same account and
//! recompiles the project. The control store, plan store, prompts and executor stay in Python and
//! are looked up when called.

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyTuple};

use crate::build_cli::formatted;
use crate::build_command::print;
use crate::direct_build::{Value, build, call, class, items, text, tuple};
use crate::handling::{raise_from, while_handling};
use crate::test_command::user_error;

const NATIVE: &str = "sqltide._native";
const AUTH_CONSTANTS: &str = "sqltide.auth.constants";
const AUTH_EXCEPTIONS: &str = "sqltide.auth.exceptions";
const ENTRY_CONSTANTS: &str = "sqltide.cli.entry.constants";
const PLAN_STORE: &str = "sqltide.executor.destruction.classes.relational_destruction_plan_store";
const DESTRUCTION_EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const DESTRUCTION_MODELS: &str = "sqltide.executor.destruction.models";
const PIPELINE_PREFIX: &str = "pipeline:";
const SUCCESS_OUTCOME: &str = "succeeded";

// ---------------------------------------------------------------------------------------------
// Authorization

/// Resolve an active built-in administrator from the OS username.
fn require_destruction_admin<'py>(
    store: &Value<'py>,
    os_username: &Value<'py>,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = store.py();
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    arguments.set_item("username", os_username)?;
    let account: Value<'py> = store.call_method("get_user_by_username", (), Some(&arguments))?;
    if account.is_none() {
        return Err(user_error(
            py,
            format!(
                "OS user '{}' is not registered in the SQLTide control store",
                formatted(os_username)?
            ),
        )?);
    }
    if !account.getattr("is_active")?.is_truthy()? {
        return Err(user_error(
            py,
            format!(
                "SQLTide account '{}' is inactive",
                formatted(&account.getattr("username")?)?
            ),
        )?);
    }
    let admin_role: Value<'py> = class(py, AUTH_CONSTANTS, "ADMIN_ROLE")?;
    if !account.getattr("roles")?.contains(&admin_role)? {
        return Err(user_error(
            py,
            format!(
                "SQLTide account '{}' requires the built-in '{}' role",
                formatted(&account.getattr("username")?)?,
                formatted(&admin_role)?
            ),
        )?);
    }
    Ok(account)
}

/// Reauthorize the plan creator without accepting same-name account replacement.
fn require_same_destruction_admin<'py>(
    store: &Value<'py>,
    os_username: &Value<'py>,
    expected_user_id: &Value<'py>,
) -> PyResult<Value<'py>> {
    let account: Value<'py> = require_destruction_admin(store, os_username)?;
    if account.getattr("user_id")?.ne(expected_user_id)? {
        return Err(user_error(
            store.py(),
            format!(
                "SQLTide account '{}' changed identity during destruction review",
                formatted(&account.getattr("username")?)?
            ),
        )?);
    }
    Ok(account)
}

// ---------------------------------------------------------------------------------------------
// Rendering and review

/// `", ".join(values) if values else "none"`.
fn values(py: Python<'_>, values: &Value<'_>) -> PyResult<String> {
    if !values.is_truthy()? {
        return Ok("none".to_owned());
    }
    pyo3::types::PyString::new(py, ", ")
        .call_method1("join", (values,))?
        .extract()
}

fn yes_no(value: &Value<'_>) -> PyResult<&'static str> {
    Ok(if value.is_truthy()? { "yes" } else { "no" })
}

fn drop_limit(plan: &Value<'_>, value: &Value<'_>) -> PyResult<String> {
    if !plan
        .getattr("relation_drop_size_policy_observed")?
        .is_truthy()?
    {
        return Ok("unknown; create a fresh plan".to_owned());
    }
    if value.is_none() {
        return Ok("unlimited".to_owned());
    }
    Ok(format!("{} bytes", formatted(value)?))
}

fn optional_number(value: &Value<'_>) -> PyResult<String> {
    if value.is_none() {
        return Ok("unknown".to_owned());
    }
    value.str()?.extract()
}

fn field(value: &Value<'_>, name: &str) -> PyResult<String> {
    formatted(&value.getattr(name)?)
}

fn named_values(py: Python<'_>, value: &Value<'_>, name: &str) -> PyResult<String> {
    values(py, &value.getattr(name)?)
}

/// Render every frozen impact-plan field before asking for review.
#[pyfunction]
#[pyo3(signature = (plan))]
fn render_destruction_plan(py: Python<'_>, plan: &Value<'_>) -> PyResult<String> {
    let mut lines: Vec<String> = vec![
        "Destruction Plan".to_owned(),
        format!("Plan ID: {}", field(plan, "plan_id")?),
        format!(
            "Operation: {}",
            formatted(&plan.getattr("operation")?.getattr("value")?)?
        ),
        format!("Target: {}", field(plan, "target")?),
        format!("Database: {}", field(plan, "database")?),
        format!("Metadata database: {}", field(plan, "metadata_database")?),
        format!(
            "Created at: {}",
            formatted(&plan.getattr("created_at")?.call_method0("isoformat")?)?
        ),
        format!(
            "Expires at: {}",
            formatted(&plan.getattr("expires_at")?.call_method0("isoformat")?)?
        ),
        format!(
            "Manifest fingerprint: {}",
            field(plan, "manifest_fingerprint")?
        ),
        format!("Plan fingerprint: {}", field(plan, "plan_fingerprint")?),
        format!(
            "Requested pipelines: {}",
            named_values(py, plan, "requested_pipeline_names")?
        ),
        format!(
            "Included dependent pipelines: {}",
            named_values(py, plan, "included_dependent_pipeline_names")?
        ),
        format!(
            "Affected pipelines: {}",
            named_values(py, plan, "affected_pipeline_names")?
        ),
        format!(
            "Affected models: {}",
            named_values(py, plan, "affected_model_names")?
        ),
        format!(
            "Affected sources: {}",
            named_values(py, plan, "affected_source_names")?
        ),
        format!(
            "Includes historical orphans: {}",
            yes_no(&plan.getattr("include_orphans")?)?
        ),
        format!(
            "Preserves managed sources: {}",
            yes_no(&plan.getattr("preserves_sources")?)?
        ),
        format!(
            "Preserves replay data: {}",
            yes_no(&plan.getattr("preserves_replay_data")?)?
        ),
        format!(
            "Estimated active-part bytes: {}",
            field(plan, "estimated_bytes")?
        ),
        format!(
            "Effective relation DROP limit: {}",
            drop_limit(plan, &plan.getattr("relation_drop_size_limit")?)?
        ),
        format!(
            "ClickHouse server default DROP limit: {}",
            drop_limit(plan, &plan.getattr("relation_drop_size_server_limit")?)?
        ),
        format!(
            "SQLTide destruction DROP override: {}",
            drop_limit(plan, &plan.getattr("relation_drop_size_override")?)?
        ),
        "Irreversible: yes; dropped data cannot be restored by SQLTide".to_owned(),
        "Recreation: authored definitions remain and a later build can recreate defined \
         resources without restoring their dropped data"
            .to_owned(),
        String::new(),
        "Relations:".to_owned(),
    ];
    let relations: Value<'_> = plan.getattr("relations")?;
    if !relations.is_truthy()? {
        lines.push("- none".to_owned());
    }
    for relation in items(&plan.getattr("relations")?)? {
        let fingerprint: Value<'_> = relation.getattr("catalog_fingerprint")?;
        let fingerprint: String = if fingerprint.is_truthy()? {
            formatted(&fingerprint)?
        } else {
            "<none>".to_owned()
        };
        let mut ownership: Vec<Value<'_>> = Vec::new();
        for item in items(&relation.getattr("ownership")?)? {
            ownership.push(item.getattr("value")?);
        }
        lines.extend([
            format!(
                "- {}.{}",
                field(&relation, "database")?,
                field(&relation, "name")?
            ),
            format!("  Kind: {}", field(&relation, "kind")?),
            format!("  Exists: {}", yes_no(&relation.getattr("exists")?)?),
            format!(
                "  Active-part bytes: {}",
                optional_number(&relation.getattr("total_bytes")?)?
            ),
            format!(
                "  Active parts: {}",
                optional_number(&relation.getattr("active_parts")?)?
            ),
            format!("  Catalog fingerprint: {fingerprint}"),
            format!(
                "  Logical names: {}",
                named_values(py, &relation, "logical_names")?
            ),
            format!(
                "  Pipelines: {}",
                named_values(py, &relation, "pipeline_names")?
            ),
            format!("  Ownership: {}", values(py, &tuple(py, ownership)?)?),
            format!(
                "  Dependencies: {}",
                named_values(py, &relation, "dependency_relation_names")?
            ),
        ]);
    }
    lines.extend([String::new(), "Required typed challenges:".to_owned()]);
    for challenge in items(&plan.getattr("challenges")?)? {
        lines.push(format!("- {}", formatted(&challenge)?));
    }
    Ok(lines.join("\n"))
}

fn input<'py>(py: Python<'py>, prompt: &str) -> PyResult<Value<'py>> {
    py.import("builtins")?.getattr("input")?.call1((prompt,))
}

/// Apply a review gate separate from the exact typed challenges.
fn confirm_destruction_review(py: Python<'_>) -> PyResult<bool> {
    let answer: Value<'_> = input(
        py,
        "Have you reviewed the complete destruction plan? [y/N] ",
    )?
    .call_method0("strip")?
    .call_method0("lower")?;
    class(py, ENTRY_CONSTANTS, "AFFIRMATIVE_RESPONSES")?.contains(answer)
}

/// Read exact challenge values without stripping or otherwise normalizing them.
#[pyfunction]
#[pyo3(signature = (plan))]
fn read_destruction_challenges<'py>(py: Python<'py>, plan: &Value<'py>) -> PyResult<Value<'py>> {
    let mut responses: Vec<Value<'py>> = Vec::new();
    for challenge in items(&plan.getattr("challenges")?)? {
        responses.push(input(
            py,
            &format!("Type {} to confirm destruction: ", formatted(&challenge)?),
        )?);
    }
    tuple(py, responses)
}

/// Render one terminal destructive-operation result.
fn print_destruction_result(py: Python<'_>, result: &Value<'_>) -> PyResult<()> {
    print(
        py,
        &text(py, &format!("Destruction {}.", field(result, "outcome")?)),
        false,
    )?;
    print(
        py,
        &text(
            py,
            &format!("Invocation ID: {}", field(result, "invocation_id")?),
        ),
        false,
    )?;
    let error_message: Value<'_> = result.getattr("error_message")?;
    if !error_message.is_none() {
        print(py, &error_message, true)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// Planning and execution

/// Pipeline names from `pipeline:NAME` selectors.
fn pipeline_names<'py>(py: Python<'py>, selectors: &Value<'py>) -> PyResult<Value<'py>> {
    let selectors: Vec<String> = items(selectors)?
        .iter()
        .map(|selector| selector.extract())
        .collect::<PyResult<_>>()?;
    if selectors
        .iter()
        .any(|selector| !selector.starts_with(PIPELINE_PREFIX) || selector == PIPELINE_PREFIX)
    {
        return Err(user_error(
            py,
            "sqt destroy selections must use a non-empty pipeline:NAME selector".to_owned(),
        )?);
    }
    tuple(
        py,
        selectors
            .iter()
            .map(|selector| text(py, &selector[PIPELINE_PREFIX.len()..]))
            .collect(),
    )
}

fn analyze<'py>(
    options: &Value<'py>,
    loaded_project: Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<Value<'py>> {
    call(
        options.py(),
        NATIVE,
        "analyze_project",
        &[
            ("pipelines_root", options.getattr("pipelines_root")?),
            ("loaded_project", loaded_project),
            ("adapter_profile", adapter_profile.clone()),
        ],
    )
}

fn default_database<'py>(options: &Value<'py>, analysis: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = options.py();
    let pipelines: Value<'py> = pyo3::types::PyList::new(
        py,
        items(&analysis.getattr("compile_inputs")?.getattr("pipelines")?)?,
    )?
    .into_any();
    crate::cli::invocation::resolve_default_database(py, &pipelines, options.getattr("database")?)
}

fn plan_destruction<'py>(
    request: &Value<'py>,
    analysis: Value<'py>,
    connection: &Value<'py>,
) -> PyResult<Value<'py>> {
    call(
        request.py(),
        NATIVE,
        "plan_destruction",
        &[
            ("request", request.clone()),
            ("analysis", analysis),
            ("connection", connection.clone()),
        ],
    )
}

/// What the replan callback needs, held across the Python call that invokes it.
struct Replan {
    options: Py<PyAny>,
    control_store: Py<PyAny>,
    os_username: Py<PyAny>,
    creator_user_id: Py<PyAny>,
    adapter_profile: Py<PyAny>,
    request: Py<PyAny>,
    connection: Py<PyAny>,
}

impl Replan {
    /// Reauthorize the creator, recompile the project from disk and plan again, failing when
    /// the physical target database moved after review.
    fn plan<'py>(&self, py: Python<'py>) -> PyResult<Value<'py>> {
        let options: Value<'py> = self.options.bind(py).clone();
        require_same_destruction_admin(
            self.control_store.bind(py),
            self.os_username.bind(py),
            self.creator_user_id.bind(py),
        )?;
        let environment: Value<'py> = options.getattr("environment")?;
        let loaded_project: Value<'py> = call(
            py,
            NATIVE,
            "load_project_input_for_path",
            &[
                ("path", options.getattr("project_dir")?),
                ("selected_target", options.getattr("selected_target")?),
                (
                    "cli_variables",
                    py.import("builtins")?
                        .getattr("dict")?
                        .call1((options.getattr("cli_variables")?,))?,
                ),
                (
                    "environment",
                    if environment.is_none() {
                        PyDict::new(py).into_any()
                    } else {
                        environment
                    },
                ),
            ],
        )?;
        let fresh_analysis: Value<'py> =
            analyze(&options, loaded_project, self.adapter_profile.bind(py))?;
        let fresh_database: Value<'py> = default_database(&options, &fresh_analysis)?;
        let request: &Value<'py> = self.request.bind(py);
        if fresh_database.ne(request.getattr("database")?)? {
            return Err(PyErr::from_value(
                class(py, DESTRUCTION_EXCEPTIONS, "DestructionDriftError")?
                    .call1(("Destruction target physical database changed after plan review",))?,
            ));
        }
        plan_destruction(request, fresh_analysis, self.connection.bind(py))
    }
}

/// `store.close()` in a `finally` clause after `outcome`.
fn closed<T>(store: &Value<'_>, outcome: PyResult<T>) -> PyResult<T> {
    let py: Python<'_> = store.py();
    if let Err(failure) = store.call_method0("close") {
        return Err(match &outcome {
            Err(pending) => while_handling(py, failure, pending),
            Ok(_) => failure,
        });
    }
    outcome
}

/// The authorized administrator and control store a destruction runs for.
struct Authorized<'a, 'py> {
    account: Value<'py>,
    control_store: &'a Value<'py>,
    os_username: &'a Value<'py>,
}

/// Plan and execute destruction for one persisted administrator.
fn run_authorized_destruction<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    authorized: Authorized<'_, 'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<i64> {
    let creator_user_id: Value<'py> = authorized.account.getattr("user_id")?;
    let actor_id: Value<'py> = creator_user_id.str()?.into_any();
    let pipeline_names: Value<'py> = pipeline_names(py, &options.getattr("selectors")?)?;
    let analysis: Value<'py> = analyze(options, loaded_project.clone(), adapter_profile)?;
    let database: Value<'py> = default_database(options, &analysis)?;
    let request: Value<'py> = build(
        py,
        DESTRUCTION_MODELS,
        "DestructionRequest",
        &[
            ("operation", options.getattr("operation")?),
            ("target", options.getattr("selected_target")?),
            ("database", database.clone()),
            ("metadata_database", database),
            ("pipeline_names", pipeline_names),
            ("include_orphans", options.getattr("include_orphans")?),
        ],
    )?;
    let plan: Value<'py> = plan_destruction(&request, analysis, client)?;
    let control_store_url: Value<'py> = options.getattr("control_store_url")?;
    let plan_store_url: Value<'py> = if control_store_url
        .extract::<String>()
        .is_ok_and(|url| crate::dev_server::create::SQLITE_MEMORY_URLS.contains(&url.as_str()))
    {
        call(
            py,
            NATIVE,
            "default_control_store_url",
            &[("project_dir", options.getattr("project_dir")?)],
        )?
    } else {
        control_store_url
    };
    let store: Value<'py> = build(
        py,
        PLAN_STORE,
        "RelationalDestructionPlanStore",
        &[("url", plan_store_url)],
    )?;
    let outcome: PyResult<i64> = (|| {
        let actor: Bound<'py, PyDict> = PyDict::new(py);
        actor.set_item("plan", &plan)?;
        actor.set_item("actor", &actor_id)?;
        store.call_method("save", (), Some(&actor))?;
        print(py, &text(py, &render_destruction_plan(py, &plan)?), false)?;
        if !confirm_destruction_review(py)? {
            print(py, &text(py, "Destruction cancelled."), false)?;
            return Ok(1);
        }
        let review: Bound<'py, PyDict> = PyDict::new(py);
        review.set_item("plan_id", plan.getattr("plan_id")?)?;
        review.set_item("actor", &actor_id)?;
        let reviewed_at: Value<'py> = store.call_method("mark_reviewed", (), Some(&review))?;
        let responses: Value<'py> = read_destruction_challenges(py, &plan)?;
        let replan: Replan = Replan {
            options: options.clone().unbind(),
            control_store: authorized.control_store.clone().unbind(),
            os_username: authorized.os_username.clone().unbind(),
            creator_user_id: creator_user_id.clone().unbind(),
            adapter_profile: adapter_profile.clone().unbind(),
            request: request.clone().unbind(),
            connection: client.clone().unbind(),
        };
        let replan: Value<'py> = PyCFunction::new_closure(
            py,
            None,
            None,
            move |arguments: &Bound<'_, PyTuple>,
                  _keywords: Option<&Bound<'_, PyDict>>|
                  -> PyResult<Py<PyAny>> { Ok(replan.plan(arguments.py())?.unbind()) },
        )?
        .into_any();
        let account: Value<'py> = require_same_destruction_admin(
            authorized.control_store,
            authorized.os_username,
            &creator_user_id,
        )?;
        let result: Value<'py> = call(
            py,
            NATIVE,
            "execute_destruction",
            &[
                ("frozen_plan", plan.clone()),
                (
                    "actor",
                    build(
                        py,
                        DESTRUCTION_MODELS,
                        "DestructionActor",
                        &[
                            ("actor_id", actor_id.clone()),
                            ("actor_name", account.getattr("username")?),
                        ],
                    )?,
                ),
                ("challenge_responses", responses),
                ("reviewed_at", reviewed_at),
                ("store", store.clone()),
                ("connection", client.clone()),
                ("observation_connection", observation_client.clone()),
                ("project_dir", options.getattr("project_dir")?),
                ("replan", replan),
            ],
        )?;
        print_destruction_result(py, &result)?;
        Ok(i64::from(!result.getattr("outcome")?.eq(SUCCESS_OUTCOME)?))
    })();
    closed(&store, outcome)
}

/// `raise CliUserError(str(error)) from error` for a control-store failure, otherwise `error`.
/// A failure while building the user-facing error is chained to the store failure.
fn user_facing(py: Python<'_>, error: PyErr) -> PyErr {
    let converted = || -> PyResult<PyErr> {
        if !error.is_instance(py, &class(py, AUTH_EXCEPTIONS, "ControlStoreError")?) {
            return Ok(error.clone_ref(py));
        }
        let handled: Value<'_> = error.value(py).clone().into_any();
        let message: Value<'_> = handled.str()?.into_any();
        let raised: Value<'_> =
            class(py, "sqltide.cli.entry.exceptions", "CliUserError")?.call1((message,))?;
        raise_from(&raised, &handled, Some(&handled))
    };
    match converted() {
        Ok(converted) => converted,
        Err(failure) => while_handling(py, failure, &error),
    }
}

/// Authorize and execute an interactive, single-use destruction plan.
#[pyfunction]
#[pyo3(signature = (*, options, client, observation_client, loaded_project, adapter_profile))]
fn run_destruction<'py>(
    py: Python<'py>,
    options: &Value<'py>,
    client: &Value<'py>,
    observation_client: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
) -> PyResult<i64> {
    let control_store: Value<'py> = match build(
        py,
        NATIVE,
        "ControlStore",
        &[("url", options.getattr("control_store_url")?)],
    ) {
        Ok(store) => store,
        Err(error) => return Err(user_facing(py, error)),
    };
    let outcome: PyResult<i64> = (|| {
        let os_username: Value<'py> = py.import("getpass")?.getattr("getuser")?.call0()?;
        let account: Value<'py> = require_destruction_admin(&control_store, &os_username)?;
        run_authorized_destruction(
            py,
            options,
            Authorized {
                account,
                control_store: &control_store,
                os_username: &os_username,
            },
            client,
            observation_client,
            loaded_project,
            adapter_profile,
        )
    })();
    let outcome: PyResult<i64> = match outcome {
        Err(error) => Err(user_facing(py, error)),
        ok => ok,
    };
    closed(&control_store, outcome)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_destruction, module)?)?;
    module.add_function(wrap_pyfunction!(render_destruction_plan, module)?)?;
    module.add_function(wrap_pyfunction!(read_destruction_challenges, module)?)
}
