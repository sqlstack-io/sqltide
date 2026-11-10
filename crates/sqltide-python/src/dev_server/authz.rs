//! Compiled-policy authorization for dev-server operations, answered with structured 403s.

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sqltide_json::JsonValue;

use super::app::Call;
use super::auth::{authenticated, has_admin_role, user_id};
use super::http::{Failure, Outcome, object, py_json, text};
use super::support::attr;
use crate::py_kwargs;

const ACCESS_TYPES: &str = "sqltide.compiler.access.types";
const BUILD_MODELS: &str = "sqltide.cli.build.models";
const MODEL_IDENTITY_PREFIX: &str = "model:";

/// One authorization check: the permission, its scope and the message a denial carries.
pub(crate) struct Check<'a> {
    pub(crate) permission: &'a str,
    pub(crate) grant_scope: Option<&'a str>,
    pub(crate) affected_pipelines: Vec<String>,
    pub(crate) denial_message: String,
}

fn enum_member<'py>(py: Python<'py>, class: &str, member: &str) -> PyResult<Bound<'py, PyAny>> {
    attr(py, ACCESS_TYPES, class)?.getattr(member)
}

/// The project name policy grants name: the compiled name, else the resolved project path.
fn project_name(call: &Call<'_, '_>, analysis: Option<&Bound<'_, PyAny>>) -> PyResult<String> {
    if let Some(analysis) = analysis {
        let name = analysis
            .getattr("compiled_project")?
            .getattr("project_name")?;
        if !name.is_none() {
            return name.extract();
        }
    }
    call.service(|services| &services.project_dir)
        .call_method0("resolve")?
        .call_method0("as_posix")?
        .extract()
}

fn target_name(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<String>> {
    if let Some(analysis) = analysis {
        let name = analysis
            .getattr("compiled_project")?
            .getattr("target_name")?;
        if !name.is_none() {
            return name.extract().map(Some);
        }
    }
    Ok(call.app.services.selected_target.clone())
}

fn policy<'py>(
    py: Python<'py>,
    analysis: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    match analysis {
        Some(analysis) => analysis.getattr("access_policy"),
        None => Ok(py.None().into_bound(py)),
    }
}

fn account_store<'py>(call: &Call<'py, '_>) -> Bound<'py, PyAny> {
    call.service(|services| &services.authorization)
        .getattr("store")
        .unwrap_or_else(|_| call.py.None().into_bound(call.py))
}

/// Allow the operation or answer 403 with the decision's evidence.
pub(crate) fn require(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
    check: Check<'_>,
) -> Outcome<()> {
    let py: Python<'_> = call.py;
    let authenticated = authenticated(call)?;
    let grant_scope = match check.grant_scope {
        Some(scope) => enum_member(py, "GrantScope", scope)?,
        None => py.None().into_bound(py),
    };
    let request = attr(py, "sqltide.authorization.models", "AuthorizationRequest")?.call(
        (),
        Some(&py_kwargs!(py;
            authenticated = authenticated,
            permission = enum_member(py, "Permission", check.permission)?,
            project_name = project_name(call, analysis)?,
            target_name = target_name(call, analysis)?,
            grant_scope = grant_scope,
            affected_pipelines = PyTuple::new(py, &check.affected_pipelines)?,
            policy = policy(py, analysis)?,
        )?),
    )?;
    let decision = attr(
        py,
        "sqltide.authorization.main.authorize_operation",
        "authorize_operation",
    )?
    .call(
        (),
        Some(&py_kwargs!(py; store = account_store(call), request = request)?),
    )?;
    if decision.getattr("allowed")?.is_truthy()? {
        return Ok(());
    }
    Err(Failure::detail(
        403,
        object(vec![
            ("message", text(check.denial_message)),
            (
                "permission",
                py_json(&decision.getattr("permission")?.getattr("value")?)?,
            ),
            (
                "reason",
                py_json(&decision.getattr("reason")?.getattr("value")?)?,
            ),
            ("project", py_json(&decision.getattr("project_name")?)?),
            ("target", py_json(&decision.getattr("target_name")?)?),
            (
                "missingPipelines",
                py_json(&decision.getattr("missing_pipelines")?)?,
            ),
        ]),
    ))
}

fn unresolved(message: String) -> Failure {
    Failure::detail(
        403,
        object(vec![
            ("message", text(message)),
            ("reason", text("unresolved_scope")),
        ]),
    )
}

fn target_check(permission: &'static str, message: &str) -> Check<'static> {
    Check {
        permission,
        grant_scope: Some("TARGET"),
        affected_pipelines: Vec::new(),
        denial_message: message.to_owned(),
    }
}

/// Whether the caller holds the system administrator role.
pub(crate) fn is_system_admin(call: &Call<'_, '_>) -> Outcome<bool> {
    Ok(has_admin_role(&authenticated(call)?)?)
}

pub(crate) fn require_reload(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> Outcome<()> {
    require(
        call,
        analysis,
        Check {
            permission: "PROJECT_RELOAD",
            grant_scope: Some("PROJECT"),
            affected_pipelines: Vec::new(),
            denial_message: "Project reload is not permitted".to_owned(),
        },
    )
}

pub(crate) fn require_kill(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> Outcome<()> {
    require(
        call,
        analysis,
        target_check("BUILD_KILL", "Force-killing builds is not permitted"),
    )
}

pub(crate) fn require_message_read(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
) -> Outcome<()> {
    require(
        call,
        Some(analysis),
        target_check(
            "SOURCE_MESSAGES_READ",
            "Reading raw source messages is not permitted",
        ),
    )
}

pub(crate) fn require_cleanup(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> Outcome<()> {
    require(
        call,
        analysis,
        target_check("DEPLOYMENT_CLEANUP", "Deployment cleanup is not permitted"),
    )
}

pub(crate) fn require_automation(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> Outcome<()> {
    require(
        call,
        analysis,
        target_check(
            "AUTOMATION_MANAGE",
            "Sensor automation management is not permitted",
        ),
    )
}

/// Authorize one named check; an unknown name defers to the runner's own validation.
pub(crate) fn require_check(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
    kind: &str,
    name: &str,
) -> Outcome<()> {
    let model_names: Option<Vec<String>> = if kind == "test" {
        let mut found: Option<Vec<String>> = None;
        for test_case in analysis
            .getattr("compiled_project")?
            .getattr("test_cases")?
            .try_iter()?
        {
            let test_case = test_case?;
            if named(&test_case)? == name {
                let mut models: Vec<String> = Vec::new();
                for step in test_case.getattr("target_cases")?.try_iter()? {
                    models.push(step?.getattr("target_model_name")?.extract()?);
                }
                found = Some(models);
                break;
            }
        }
        found
    } else {
        let mut found: Option<Vec<String>> = None;
        for audit in analysis
            .getattr("compiled_project")?
            .getattr("audits")?
            .try_iter()?
        {
            let audit = audit?;
            if named(&audit)? == name {
                found = Some(audit.getattr("referenced_model_names")?.extract()?);
                break;
            }
        }
        found
    };
    let Some(model_names) = model_names else {
        return Ok(());
    };
    let Some(scope) = pipelines_for_models(analysis, &model_names)? else {
        return Err(unresolved(format!(
            "Cannot resolve pipeline ownership for check '{name}'"
        )));
    };
    let permission: &str = if kind == "test" {
        "QUALITY_TEST_RUN"
    } else {
        "QUALITY_AUDIT_RUN"
    };
    require(
        call,
        Some(analysis),
        Check {
            permission,
            grant_scope: scope.is_empty().then_some("PROJECT"),
            affected_pipelines: scope,
            denial_message: format!("Running this {kind} is not permitted"),
        },
    )
}

/// `name or file_path.stem`.
fn named(item: &Bound<'_, PyAny>) -> PyResult<String> {
    let name = item.getattr("name")?;
    if name.is_truthy()? {
        return name.extract();
    }
    item.getattr("file_path")?.getattr("stem")?.extract()
}

/// The sorted pipelines owning `model_names`, or `None` when one is unknown.
fn pipelines_for_models(
    analysis: &Bound<'_, PyAny>,
    model_names: &[String],
) -> PyResult<Option<Vec<String>>> {
    let mut owner: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for model in analysis
        .getattr("compiled_project")?
        .getattr("models")?
        .try_iter()?
    {
        let model = model?;
        owner.insert(
            model.getattr("key")?.getattr("name")?.extract()?,
            model.getattr("pipeline_name")?.extract()?,
        );
    }
    let mut pipelines: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for name in model_names {
        let Some(pipeline) = owner.get(name) else {
            return Ok(None);
        };
        pipelines.insert(pipeline.clone());
    }
    Ok(Some(pipelines.into_iter().collect()))
}

/// Model names among logical ids like `model:orders`, or `None` for a malformed list.
fn models_from_logical_ids(logical_ids: &Bound<'_, PyAny>) -> PyResult<Option<Vec<String>>> {
    if !logical_ids.is_instance_of::<pyo3::types::PyList>() {
        return Ok(None);
    }
    let mut names: Vec<String> = Vec::new();
    for item in logical_ids.try_iter()? {
        let item = item?;
        let Ok(text) = item.extract::<String>() else {
            return Ok(None);
        };
        if let Some(name) = text.strip_prefix(MODEL_IDENTITY_PREFIX) {
            names.push(name.to_owned());
        }
    }
    Ok(Some(names))
}

fn pipelines_for_keys(
    analysis: &Bound<'_, PyAny>,
    keys: &Bound<'_, PyAny>,
) -> Outcome<Vec<String>> {
    let py: Python<'_> = analysis.py();
    let model_type =
        attr(py, "sqltide.compiler.compile.types", "LogicalResourceType")?.getattr("MODEL")?;
    let mut names: Vec<String> = Vec::new();
    for key in keys.try_iter()? {
        let key = key?;
        if key.getattr("resource_type")?.eq(&model_type)? {
            names.push(key.getattr("name")?.extract()?);
        }
    }
    pipelines_for_models(analysis, &names)?.ok_or_else(|| {
        unresolved("Cannot resolve pipeline ownership for the resolved build scope".to_owned())
    })
}

/// The direct half of a build preparation.
pub(crate) fn direct_part<'py>(
    preparation: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py: Python<'py> = preparation.py();
    if preparation.is_instance(&attr(py, BUILD_MODELS, "DirectWorkflowPreparation")?)? {
        return Ok(Some(preparation.clone()));
    }
    if preparation.is_instance(&attr(py, BUILD_MODELS, "MixedWorkflowPreparation")?)? {
        return Ok(Some(preparation.getattr("direct")?));
    }
    Ok(None)
}

/// The staged-deployment half of a build preparation.
pub(crate) fn virtual_part<'py>(
    preparation: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py: Python<'py> = preparation.py();
    if preparation.is_instance(&attr(py, BUILD_MODELS, "VirtualWorkflowPreparation")?)? {
        return Ok(Some(preparation.clone()));
    }
    if preparation.is_instance(&attr(py, BUILD_MODELS, "MixedWorkflowPreparation")?)? {
        return Ok(Some(preparation.getattr("virtual")?));
    }
    Ok(None)
}

/// Require complete pipeline coverage for a resolved build workflow.
pub(crate) fn require_prepared_build(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
    preparation: &Bound<'_, PyAny>,
) -> Outcome<()> {
    let direct: Vec<String> = match direct_part(preparation)? {
        Some(direct) => pipelines_for_keys(
            analysis,
            &direct
                .getattr("preview")?
                .getattr("plan")?
                .getattr("execution_scope")?,
        )?,
        None => Vec::new(),
    };
    let staged: Vec<String> = match virtual_part(preparation)? {
        Some(staged) => pipelines_for_keys(
            analysis,
            &staged.getattr("preview")?.getattr("run_execution_scope")?,
        )?,
        None => Vec::new(),
    };
    let scoped = |permission: &'static str, pipelines: Vec<String>, message: &str| {
        require(
            call,
            Some(analysis),
            Check {
                permission,
                grant_scope: None,
                affected_pipelines: pipelines,
                denial_message: message.to_owned(),
            },
        )
    };
    if direct.is_empty() && staged.is_empty() {
        return scoped(
            "BUILD_DIRECT_RUN",
            Vec::new(),
            "Build authorization requires a resolvable pipeline scope",
        );
    }
    if !direct.is_empty() {
        scoped(
            "BUILD_DIRECT_RUN",
            direct,
            "Direct build writes are not permitted",
        )?;
    }
    if !staged.is_empty() {
        scoped(
            "DEPLOYMENT_CREATE",
            staged,
            "Creating staged deployments is not permitted",
        )?;
    }
    Ok(())
}

/// Require `BUILD_CANCEL` over every pipeline the active run recorded.
pub(crate) fn require_run_cancel(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
    invocation_id: &str,
    active_runs: &Bound<'_, PyAny>,
) -> Outcome<()> {
    let mut pipelines: Option<Vec<String>> = None;
    for run in active_runs.try_iter()? {
        let run = run?;
        let matches: bool = run
            .call_method1("get", ("invocationId",))?
            .eq(invocation_id)?;
        if matches {
            let logical_ids = run.call_method1("get", ("executedLogicalIds",))?;
            pipelines = match models_from_logical_ids(&logical_ids)? {
                Some(models) => pipelines_for_models(analysis, &models)?,
                None => None,
            };
            break;
        }
    }
    let Some(pipelines) = pipelines else {
        return Err(unresolved(format!(
            "Cannot resolve pipeline scope for run '{invocation_id}'"
        )));
    };
    require(
        call,
        Some(analysis),
        Check {
            permission: "BUILD_CANCEL",
            grant_scope: None,
            affected_pipelines: pipelines,
            denial_message: "Cancelling this run is not permitted".to_owned(),
        },
    )
}

/// Require `DEPLOYMENT_PROMOTE` over every pipeline the promotion changes.
pub(crate) fn require_promotion(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
    deployment_id: &str,
    logical_ids: &Bound<'_, PyAny>,
) -> Outcome<()> {
    let list = pyo3::types::PyList::new(
        call.py,
        logical_ids.try_iter()?.collect::<PyResult<Vec<_>>>()?,
    )?;
    let pipelines: Option<Vec<String>> = match models_from_logical_ids(list.as_any())? {
        Some(models) => pipelines_for_models(analysis, &models)?,
        None => None,
    };
    let pipelines: Vec<String> = match pipelines {
        Some(pipelines) if !pipelines.is_empty() => pipelines,
        _ => {
            return Err(unresolved(format!(
                "Cannot resolve pipeline scope for deployment '{deployment_id}' promotion"
            )));
        }
    };
    require(
        call,
        Some(analysis),
        Check {
            permission: "DEPLOYMENT_PROMOTE",
            grant_scope: None,
            affected_pipelines: pipelines,
            denial_message: "Promoting this deployment is not permitted".to_owned(),
        },
    )
}

/// Destructive operations need an active system administrator and the operation's permission.
pub(crate) fn require_destruction(
    call: &Call<'_, '_>,
    analysis: &Bound<'_, PyAny>,
    operation: &str,
    affected_pipelines: Vec<String>,
) -> Outcome<()> {
    let py: Python<'_> = call.py;
    let authenticated = authenticated(call)?;
    let account = account_store(call).call_method(
        "get_user_by_id",
        (),
        Some(&py_kwargs!(py; user_id = user_id(&authenticated)?)?),
    )?;
    let allowed: bool = !account.is_none()
        && account.getattr("is_active")?.is_truthy()?
        && account.getattr("roles")?.contains("admin")?;
    if !allowed {
        return Err(Failure::detail(
            403,
            object(vec![
                (
                    "message",
                    text("Destructive operations require an active system administrator"),
                ),
                ("reason", text("system_admin_required")),
            ]),
        ));
    }
    // `DestructionOperation(operation)` rejects an unknown operation with ValueError.
    let operation = attr(
        py,
        "sqltide.executor.destruction.types",
        "DestructionOperation",
    )?
    .call1((operation,))?;
    let reset: bool = operation.getattr("value")?.extract::<String>()? == "reset_target";
    require(
        call,
        Some(analysis),
        Check {
            permission: if reset {
                "TARGET_RESET"
            } else {
                "PIPELINE_DESTROY"
            },
            grant_scope: reset.then_some("TARGET"),
            affected_pipelines: if reset {
                Vec::new()
            } else {
                affected_pipelines
            },
            denial_message: if reset {
                "Resetting this target is not permitted".to_owned()
            } else {
                "Destroying these pipelines is not permitted".to_owned()
            },
        },
    )
}

/// The caller's effective capabilities; destructive permissions show only to administrators.
pub(crate) fn capabilities_payload(
    call: &Call<'_, '_>,
    analysis: Option<&Bound<'_, PyAny>>,
) -> Outcome<JsonValue> {
    let py: Python<'_> = call.py;
    let authenticated = authenticated(call)?;
    let mut pipeline_names: Vec<String> = Vec::new();
    if let Some(analysis) = analysis {
        for item in analysis
            .getattr("compiled_project")?
            .getattr("pipelines")?
            .try_iter()?
        {
            pipeline_names.push(item?.getattr("pipeline")?.getattr("name")?.extract()?);
        }
        pipeline_names.sort();
    }
    let request = attr(py, "sqltide.authorization.models", "CapabilityRequest")?.call(
        (),
        Some(&py_kwargs!(py;
            authenticated = authenticated,
            project_name = project_name(call, analysis)?,
            target_name = target_name(call, analysis)?,
            pipeline_names = PyTuple::new(py, &pipeline_names)?,
            policy = policy(py, analysis)?,
        )?),
    )?;
    let capabilities = attr(
        py,
        "sqltide.authorization.main._effective_capabilities",
        "effective_capabilities",
    )?
    .call(
        (),
        Some(&py_kwargs!(py; store = account_store(call), request = request)?),
    )?;
    let system_admin: bool = capabilities.getattr("system_admin")?.is_truthy()?;
    let hidden = |permission: &Bound<'_, PyAny>| -> PyResult<bool> {
        let value: String = permission.getattr("value")?.extract()?;
        Ok(!system_admin && (value == "pipeline.destroy" || value == "target.reset"))
    };
    let mut permissions: Vec<JsonValue> = Vec::new();
    for permission in capabilities.getattr("permissions")?.try_iter()? {
        let permission = permission?;
        if !hidden(&permission)? {
            permissions.push(py_json(&permission.getattr("value")?)?);
        }
    }
    let mut pipeline_permissions: Vec<(sqltide_json::JsonKey, JsonValue)> = Vec::new();
    for entry in capabilities.getattr("pipeline_permissions")?.try_iter()? {
        let entry = entry?;
        let permission = entry.get_item(0)?;
        if hidden(&permission)? {
            continue;
        }
        let key: String = permission.getattr("value")?.extract()?;
        let value: JsonValue = py_json(&entry.get_item(1)?)?;
        // A dict comprehension keeps the last value of a repeated key at its first position.
        match pipeline_permissions
            .iter_mut()
            .find(|(existing, _)| *existing == sqltide_json::JsonKey::Text(key.clone()))
        {
            Some((_, existing)) => *existing = value,
            None => pipeline_permissions.push((sqltide_json::JsonKey::Text(key), value)),
        }
    }
    Ok(object(vec![
        ("systemAdmin", JsonValue::Bool(system_admin)),
        ("project", py_json(&capabilities.getattr("project_name")?)?),
        ("target", py_json(&capabilities.getattr("target_name")?)?),
        ("permissions", JsonValue::Array(permissions)),
        (
            "pipelinePermissions",
            JsonValue::Object(pipeline_permissions),
        ),
        (
            "staleRoles",
            py_json(&capabilities.getattr("stale_roles")?)?,
        ),
    ]))
}

/// The compiled read-only project role policy.
pub(crate) fn access_policy_payload(analysis: Option<&Bound<'_, PyAny>>) -> PyResult<JsonValue> {
    let policy = match analysis {
        Some(analysis) => analysis.getattr("access_policy")?,
        None => return Ok(empty_policy()),
    };
    if policy.is_none() {
        return Ok(empty_policy());
    }
    let mut roles: Vec<JsonValue> = Vec::new();
    for role in policy.getattr("roles")?.try_iter()? {
        let role = role?;
        let mut grants: Vec<JsonValue> = Vec::new();
        for grant in role.getattr("grants")?.try_iter()? {
            let grant = grant?;
            let scope = grant.getattr("scope")?;
            let mut permissions: Vec<JsonValue> = Vec::new();
            for permission in grant.getattr("permissions")?.try_iter()? {
                permissions.push(py_json(&permission?.getattr("value")?)?);
            }
            grants.push(object(vec![
                (
                    "scope",
                    if scope.is_none() {
                        JsonValue::Null
                    } else {
                        py_json(&scope.getattr("value")?)?
                    },
                ),
                ("pipelines", py_json(&grant.getattr("pipelines")?)?),
                ("permissions", JsonValue::Array(permissions)),
            ]));
        }
        roles.push(object(vec![
            ("name", py_json(&role.getattr("name")?)?),
            ("description", py_json(&role.getattr("description")?)?),
            ("grants", JsonValue::Array(grants)),
        ]));
    }
    Ok(object(vec![
        ("present", JsonValue::Bool(true)),
        ("fingerprint", py_json(&policy.getattr("fingerprint")?)?),
        ("roles", JsonValue::Array(roles)),
    ]))
}

fn empty_policy() -> JsonValue {
    object(vec![
        ("present", JsonValue::Bool(false)),
        ("fingerprint", JsonValue::Null),
        ("roles", JsonValue::Array(Vec::new())),
    ])
}
