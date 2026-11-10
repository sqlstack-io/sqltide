//! Evaluate current project-role assignments against one compiled access policy.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use sqltide_config::access::{PIPELINE_PERMISSIONS, PROJECT_PERMISSIONS, TARGET_PERMISSIONS};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const ADMIN_ROLE: &str = "admin";
const PROJECT_SCOPE: &str = "project";
const TARGET_SCOPE: &str = "target";
const ACCESS_TYPES: &str = "sqltide.compiler.access.types";
const AUTHORIZATION_MODELS: &str = "sqltide.authorization.models";
const AUTHORIZATION_TYPES: &str = "sqltide.authorization.types";

/// One compiled grant: permissions on named pipelines (no scope) or at a scope.
struct Grant {
    scope: Option<String>,
    permissions: BTreeSet<String>,
    pipelines: Vec<String>,
}

struct Role {
    name: String,
    grants: Vec<Grant>,
}

struct Assignment {
    role_name: String,
}

/// The outcome of one decision before it becomes an `AuthorizationDecision`.
struct Decision {
    allowed: bool,
    reason: &'static str,
    affected_pipelines: Vec<String>,
    matched_roles: Vec<String>,
    missing_pipelines: Vec<String>,
}

struct Capabilities {
    system_admin: bool,
    permissions: Vec<&'static str>,
    pipeline_permissions: Vec<(&'static str, Vec<String>)>,
    stale_roles: Vec<String>,
}

fn sorted_permissions(groups: &[&[&'static str]]) -> Vec<&'static str> {
    let unique: BTreeSet<&'static str> = groups
        .iter()
        .flat_map(|group| group.iter().copied())
        .collect();
    unique.into_iter().collect()
}

fn scoped_grant_matches(grant: &Grant, permission: &str, requested_scope: &str) -> bool {
    if !grant.permissions.contains(permission) {
        return false;
    }
    match grant.scope.as_deref() {
        Some(scope) if scope == requested_scope => true,
        Some(PROJECT_SCOPE) => requested_scope == TARGET_SCOPE,
        _ => false,
    }
}

/// A complete, side-effect-free authorization decision for one non-admin request.
fn decide(
    permission: &str,
    grant_scope: Option<&str>,
    affected: &[String],
    roles: Option<&[Role]>,
    assignments: &[Assignment],
) -> Decision {
    let affected_pipelines: Vec<String> = affected
        .iter()
        .cloned()
        .collect::<BTreeSet<String>>()
        .into_iter()
        .collect();
    let decision = |allowed: bool,
                    reason: &'static str,
                    matched: Vec<String>,
                    missing: Vec<String>| Decision {
        allowed,
        reason,
        affected_pipelines: affected_pipelines.clone(),
        matched_roles: matched,
        missing_pipelines: missing,
    };
    let Some(roles) = roles else {
        return decision(false, "POLICY_ABSENT", Vec::new(), Vec::new());
    };
    if assignments.is_empty() {
        return decision(
            false,
            "NO_MATCHING_ASSIGNMENT",
            Vec::new(),
            affected_pipelines.clone(),
        );
    }
    let role_by_name: HashMap<&str, &Role> = roles
        .iter()
        .map(|role| (role.name.as_str(), role))
        .collect();
    let assigned_roles: Vec<&Role> = assignments
        .iter()
        .filter_map(|assignment| role_by_name.get(assignment.role_name.as_str()).copied())
        .collect();
    if assigned_roles.is_empty() {
        return decision(
            false,
            "STALE_ASSIGNMENT",
            Vec::new(),
            affected_pipelines.clone(),
        );
    }
    if let Some(requested_scope) = grant_scope {
        let matching: Vec<String> = assigned_roles
            .iter()
            .filter(|role| {
                role.grants
                    .iter()
                    .any(|grant| scoped_grant_matches(grant, permission, requested_scope))
            })
            .map(|role| role.name.clone())
            .collect::<BTreeSet<String>>()
            .into_iter()
            .collect();
        let allowed: bool = !matching.is_empty();
        let reason: &'static str = if allowed {
            "GRANTED"
        } else {
            "MISSING_PERMISSION"
        };
        return decision(allowed, reason, matching, Vec::new());
    }
    if affected_pipelines.is_empty() {
        return decision(false, "EMPTY_PIPELINE_SCOPE", Vec::new(), Vec::new());
    }
    let mut covered_by_role: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for role in &assigned_roles {
        let mut covered: BTreeSet<String> = BTreeSet::new();
        for grant in &role.grants {
            if !grant.permissions.contains(permission) {
                continue;
            }
            match grant.scope.as_deref() {
                None => covered.extend(
                    grant
                        .pipelines
                        .iter()
                        .filter(|pipeline| affected_pipelines.contains(pipeline))
                        .cloned(),
                ),
                Some(PROJECT_SCOPE) => covered.extend(affected_pipelines.iter().cloned()),
                Some(_) => {}
            }
        }
        if !covered.is_empty() {
            covered_by_role.insert(role.name.clone(), covered);
        }
    }
    let covered: BTreeSet<&String> = covered_by_role.values().flatten().collect();
    let missing: Vec<String> = affected_pipelines
        .iter()
        .filter(|pipeline| !covered.contains(pipeline))
        .cloned()
        .collect();
    let allowed: bool = missing.is_empty();
    let reason: &'static str = if allowed {
        "GRANTED"
    } else {
        "MISSING_PIPELINES"
    };
    decision(
        allowed,
        reason,
        covered_by_role.into_keys().collect(),
        missing,
    )
}

/// The caller's effective operational permissions, for a non-admin caller.
fn capabilities(
    roles: Option<&[Role]>,
    assignments: &[Assignment],
    pipeline_names: &[String],
) -> Capabilities {
    let role_by_name: HashMap<&str, &Role> = roles
        .unwrap_or_default()
        .iter()
        .map(|role| (role.name.as_str(), role))
        .collect();
    let assigned_names: BTreeSet<&str> = assignments
        .iter()
        .map(|assignment| assignment.role_name.as_str())
        .collect();
    let grants: Vec<&Grant> = assigned_names
        .iter()
        .filter_map(|name| role_by_name.get(name))
        .flat_map(|role| role.grants.iter())
        .collect();
    let permissions: Vec<&'static str> =
        sorted_permissions(&[&PROJECT_PERMISSIONS, &TARGET_PERMISSIONS])
            .into_iter()
            .filter(|permission| {
                grants
                    .iter()
                    .any(|grant| grant.scope.is_some() && grant.permissions.contains(*permission))
            })
            .collect();
    let mut pipeline_permissions: Vec<(&'static str, Vec<String>)> = Vec::new();
    for permission in sorted_permissions(&[&PIPELINE_PERMISSIONS]) {
        let mut covered: BTreeSet<String> = BTreeSet::new();
        for grant in grants
            .iter()
            .filter(|grant| grant.permissions.contains(permission))
        {
            match grant.scope.as_deref() {
                Some(PROJECT_SCOPE) => covered.extend(pipeline_names.iter().cloned()),
                None => covered.extend(grant.pipelines.iter().cloned()),
                Some(_) => {}
            }
        }
        if !covered.is_empty() {
            pipeline_permissions.push((permission, covered.into_iter().collect()));
        }
    }
    Capabilities {
        system_admin: false,
        permissions,
        pipeline_permissions,
        stale_roles: assigned_names
            .into_iter()
            .filter(|name| !role_by_name.contains_key(name))
            .map(str::to_owned)
            .collect(),
    }
}

fn text_option(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    Ok(Some(value.str()?.to_string()))
}

fn texts(value: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    value
        .try_iter()?
        .map(|item| Ok(item?.str()?.to_string()))
        .collect()
}

/// The roles of `request.policy`, or `None` when the project has no access policy.
fn policy_roles(request: &Bound<'_, PyAny>) -> PyResult<Option<Vec<Role>>> {
    let policy: Bound<'_, PyAny> = request.getattr("policy")?;
    if policy.is_none() {
        return Ok(None);
    }
    let mut roles: Vec<Role> = Vec::new();
    for role in policy.getattr("roles")?.try_iter()? {
        let role: Bound<'_, PyAny> = role?;
        let mut grants: Vec<Grant> = Vec::new();
        for grant in role.getattr("grants")?.try_iter()? {
            let grant: Bound<'_, PyAny> = grant?;
            grants.push(Grant {
                scope: text_option(&grant.getattr("scope")?)?,
                permissions: texts(&grant.getattr("permissions")?)?.into_iter().collect(),
                pipelines: texts(&grant.getattr("pipelines")?)?,
            });
        }
        roles.push(Role {
            name: role.getattr("name")?.str()?.to_string(),
            grants,
        });
    }
    Ok(Some(roles))
}

/// The caller's current role assignments that apply to the request's target.
fn assignments(store: &Bound<'_, PyAny>, request: &Bound<'_, PyAny>) -> PyResult<Vec<Assignment>> {
    let py: Python<'_> = store.py();
    let target_name: Option<String> = text_option(&request.getattr("target_name")?)?;
    let listed: Bound<'_, PyAny> = store.call_method(
        "list_project_role_assignments",
        (),
        Some(&py_kwargs!(py;
            user_id = request.getattr("authenticated")?.getattr("principal")?.getattr("user_id")?,
            project_name = request.getattr("project_name")?,
        )?),
    )?;
    let mut applicable: Vec<Assignment> = Vec::new();
    for assignment in listed.try_iter()? {
        let assignment: Bound<'_, PyAny> = assignment?;
        let assigned_target: Option<String> = text_option(&assignment.getattr("target_name")?)?;
        if assigned_target.is_none() || assigned_target == target_name {
            applicable.push(Assignment {
                role_name: assignment.getattr("role_name")?.str()?.to_string(),
            });
        }
    }
    Ok(applicable)
}

fn is_admin(request: &Bound<'_, PyAny>) -> PyResult<bool> {
    request
        .getattr("authenticated")?
        .getattr("roles")?
        .contains(ADMIN_ROLE)
}

fn permission_member<'py>(py: Python<'py>, value: &str) -> PyResult<Bound<'py, PyAny>> {
    attr(py, ACCESS_TYPES, "Permission")?.call1((value,))
}

/// Evaluate one authoritative operation against current membership.
#[pyfunction]
#[pyo3(signature = (*, store, request))]
fn authorize_operation<'py>(
    store: Bound<'py, PyAny>,
    request: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = store.py();
    let decision: Decision = if is_admin(&request)? {
        Decision {
            allowed: true,
            reason: "SYSTEM_ADMIN",
            affected_pipelines: texts(&request.getattr("affected_pipelines")?)?
                .into_iter()
                .collect::<BTreeSet<String>>()
                .into_iter()
                .collect(),
            matched_roles: vec![ADMIN_ROLE.to_owned()],
            missing_pipelines: Vec::new(),
        }
    } else {
        let roles: Option<Vec<Role>> = policy_roles(&request)?;
        let applicable: Vec<Assignment> = match roles {
            Some(_) => assignments(&store, &request)?,
            None => Vec::new(),
        };
        decide(
            &request.getattr("permission")?.str()?.to_string(),
            text_option(&request.getattr("grant_scope")?)?.as_deref(),
            &texts(&request.getattr("affected_pipelines")?)?,
            roles.as_deref(),
            &applicable,
        )
    };
    attr(py, AUTHORIZATION_MODELS, "AuthorizationDecision")?.call(
        (),
        Some(&py_kwargs!(py;
            allowed = decision.allowed,
            reason = attr(py, AUTHORIZATION_TYPES, "AuthorizationReason")?.getattr(decision.reason)?,
            permission = request.getattr("permission")?,
            project_name = request.getattr("project_name")?,
            target_name = request.getattr("target_name")?,
            affected_pipelines = PyTuple::new(py, decision.affected_pipelines)?,
            matched_roles = PyTuple::new(py, decision.matched_roles)?,
            missing_pipelines = PyTuple::new(py, decision.missing_pipelines)?,
        )?),
    )
}

/// Summarize one user's effective operational permissions.
#[pyfunction]
#[pyo3(signature = (*, store, request))]
fn effective_capabilities<'py>(
    store: Bound<'py, PyAny>,
    request: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = store.py();
    let pipeline_names: Vec<String> = texts(&request.getattr("pipeline_names")?)?;
    let summary: Capabilities = if is_admin(&request)? {
        let mut sorted_names: Vec<String> = pipeline_names;
        sorted_names.sort();
        Capabilities {
            system_admin: true,
            permissions: sorted_permissions(&[&PROJECT_PERMISSIONS, &TARGET_PERMISSIONS]),
            pipeline_permissions: sorted_permissions(&[&PIPELINE_PERMISSIONS])
                .into_iter()
                .map(|permission| (permission, sorted_names.clone()))
                .collect(),
            stale_roles: Vec::new(),
        }
    } else {
        let applicable: Vec<Assignment> = assignments(&store, &request)?;
        capabilities(
            policy_roles(&request)?.as_deref(),
            &applicable,
            &pipeline_names,
        )
    };
    let permissions: Vec<Bound<'py, PyAny>> = summary
        .permissions
        .iter()
        .map(|permission| permission_member(py, permission))
        .collect::<PyResult<_>>()?;
    let mut pipeline_permissions: Vec<Bound<'py, PyTuple>> = Vec::new();
    for (permission, pipelines) in summary.pipeline_permissions {
        pipeline_permissions.push(PyTuple::new(
            py,
            [
                permission_member(py, permission)?,
                PyTuple::new(py, pipelines)?.into_any(),
            ],
        )?);
    }
    attr(py, AUTHORIZATION_MODELS, "EffectiveCapabilities")?.call(
        (),
        Some(&py_kwargs!(py;
            system_admin = summary.system_admin,
            project_name = request.getattr("project_name")?,
            target_name = request.getattr("target_name")?,
            permissions = PyTuple::new(py, permissions)?,
            pipeline_permissions = PyTuple::new(py, pipeline_permissions)?,
            stale_roles = PyTuple::new(py, summary.stale_roles)?,
        )?),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(authorize_operation, module)?)?;
    module.add_function(wrap_pyfunction!(effective_capabilities, module)?)
}
