//! Janitor preview and apply: gather deployment inventory and catalog evidence, decide retention
//! natively, and remove superseded bindings and relations through the mutation gateway.

use std::collections::{BTreeSet, HashSet};

use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::dev_server::support::attr;
use crate::executor::{
    DeploymentArgument, MappingArgument, RetainedPublicationArgument, janitor_binding_activity,
    janitor_preview,
};
use crate::handling::within;
use crate::py_kwargs;
use crate::workflow_statements::numbered;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const PLANNER_CONSTANTS: &str = "sqltide.compiler.planner.constants";
const JANITOR_MODELS: &str = "sqltide.executor.janitor.models";
const WAREHOUSE_WORKFLOW: &str = "sqltide.executor.workflow.main._execute_warehouse_workflow";
const MUTATION_LOCK: &str = "sqltide.executor.workflow.main.target_mutation_lock";

type Any<'py> = Bound<'py, PyAny>;
type Binding = (String, String, String);

/// The janitor's deployment inventory, as the native retention rules read it.
struct Inventory<'py> {
    deployments: Vec<Any<'py>>,
    native_deployments: Vec<DeploymentArgument>,
    publications: Vec<RetainedPublicationArgument>,
}

fn constant<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<String> {
    attr(py, module, name)?.extract()
}

fn items<'py>(value: &Any<'py>, field: &str) -> PyResult<Vec<Any<'py>>> {
    value.getattr(field)?.try_iter()?.collect()
}

fn bindings(values: &Any<'_>) -> PyResult<Vec<Binding>> {
    values
        .try_iter()?
        .map(|binding| {
            let binding: Any<'_> = binding?;
            Ok((
                binding.getattr("database")?.extract()?,
                binding.getattr("logical_name")?.extract()?,
                binding.getattr("physical_name")?.extract()?,
            ))
        })
        .collect()
}

/// Microseconds since the Unix epoch of an aware datetime, as `(moment - epoch) // 1µs`.
fn micros(moment: &Any<'_>) -> PyResult<i64> {
    let py: Python<'_> = moment.py();
    let datetime: Any<'_> = attr(py, "datetime", "datetime")?;
    let utc: Any<'_> = attr(py, "datetime", "UTC")?;
    let epoch: Any<'_> = datetime.call((1970, 1, 1), Some(&py_kwargs!(py; tzinfo = utc)?))?;
    let microsecond: Any<'_> =
        attr(py, "datetime", "timedelta")?.call((), Some(&py_kwargs!(py; microseconds = 1)?))?;
    moment.sub(epoch)?.floor_div(microsecond)?.extract()
}

fn table_or_view(object_type: &Any<'_>) -> PyResult<bool> {
    let py: Python<'_> = object_type.py();
    Ok(
        object_type.eq(attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_TABLE")?)?
            || object_type.eq(attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_VIEW")?)?,
    )
}

fn load_inventory<'py>(
    client: &Any<'py>,
    metadata_database: &Any<'py>,
) -> PyResult<Inventory<'py>> {
    let py: Python<'py> = client.py();
    let inventory: Any<'py> =
        client.call_method1("load_deployment_inventory", (metadata_database,))?;
    let incomplete: Any<'py> = attr(
        py,
        ADAPTER_CONSTANTS,
        "VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE",
    )?;
    let deployments: Vec<Any<'py>> = items(&inventory, "deployments")?;
    let mut native_deployments: Vec<DeploymentArgument> = Vec::new();
    for deployment in &deployments {
        let mut mappings: Vec<MappingArgument> = Vec::new();
        for mapping in items(deployment, "prepared_object_mappings")? {
            let key: Any<'py> = mapping.getattr("logical_key")?;
            mappings.push((
                key.getattr("database")?.extract()?,
                table_or_view(&key.getattr("object_type")?)?,
                key.getattr("name")?.extract()?,
                mapping.getattr("physical_name")?.extract()?,
                mapping.getattr("logical_model_name")?.extract()?,
            ));
        }
        native_deployments.push((
            deployment.getattr("deployment_id")?.extract()?,
            deployment.getattr("created_at")?.extract()?,
            deployment.getattr("status")?.eq(&incomplete)?,
            mappings,
        ));
    }
    let utc: Any<'py> = attr(py, "datetime", "UTC")?;
    let mut publications: Vec<RetainedPublicationArgument> = Vec::new();
    for event in items(&inventory, "publish_events")? {
        let published_at: Any<'py> = event
            .getattr("published_at")?
            .call_method1("replace", (" ", "T"))?;
        let parsed: Any<'py> =
            attr(py, "datetime", "datetime")?.call_method1("fromisoformat", (published_at,))?;
        let as_utc: Any<'py> =
            parsed.call_method("replace", (), Some(&py_kwargs!(py; tzinfo = &utc)?))?;
        let ranked: &Any<'py> = if parsed.getattr("tzinfo")?.is_none() {
            &as_utc
        } else {
            &parsed
        };
        let deployment_id: String = event.getattr("deployment_id")?.extract()?;
        let publication_id: Any<'py> = event.getattr("publication_id")?;
        let identity: String = if publication_id.is_truthy()? {
            publication_id.extract()?
        } else {
            deployment_id.clone()
        };
        publications.push((
            deployment_id,
            (micros(ranked)?, identity),
            micros(&as_utc)?,
            event.getattr("logical_view_names")?.extract()?,
            bindings(&event.getattr("bindings")?)?,
        ));
    }
    Ok(Inventory {
        deployments,
        native_deployments,
        publications,
    })
}

/// Every relation in each database that a prepared mapping names.
fn physical_relations(
    client: &Any<'_>,
    deployments: &[Any<'_>],
    database: &Any<'_>,
) -> PyResult<HashSet<(String, String)>> {
    let mut databases: BTreeSet<String> = BTreeSet::new();
    for deployment in deployments {
        for mapping in items(deployment, "prepared_object_mappings")? {
            let mapped: Any<'_> = mapping.getattr("logical_key")?.getattr("database")?;
            let chosen: &Any<'_> = if mapped.is_truthy()? {
                &mapped
            } else {
                database
            };
            databases.insert(chosen.extract()?);
        }
    }
    let mut relations: HashSet<(String, String)> = HashSet::new();
    for mapping_database in databases {
        let names: Any<'_> = client
            .call_method1("load_catalog", (&mapping_database,))?
            .call_method0("relation_names")?;
        for name in names.try_iter()? {
            relations.insert((mapping_database.clone(), name?.extract()?));
        }
    }
    Ok(relations)
}

/// The sorted physical names a preview candidate would remove: its prepared tables plus live
/// dead-letter views a policy switch created for its materialized views.
fn candidate_objects(deployment: &Any<'_>, live: &HashSet<String>) -> PyResult<Vec<String>> {
    let py: Python<'_> = deployment.py();
    let materialized_view: Any<'_> = attr(
        py,
        COMPILE_CONSTANTS,
        "DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW",
    )?;
    let suffix: String = constant(py, PLANNER_CONSTANTS, "DIRECT_DEAD_LETTERS_VIEW_SUFFIX")?;
    let mut names: BTreeSet<String> = BTreeSet::new();
    for mapping in items(deployment, "prepared_object_mappings")? {
        let physical: String = mapping.getattr("physical_name")?.extract()?;
        let key: Any<'_> = mapping.getattr("logical_key")?;
        if key.getattr("object_type")?.eq(&materialized_view)? {
            let logical: String = key.getattr("name")?.extract()?;
            let view: String = format!(
                "{logical}{suffix}{}",
                physical.strip_prefix(logical.as_str()).unwrap_or(&physical)
            );
            if live.contains(&view) {
                names.insert(view);
            }
        }
        names.insert(physical);
    }
    Ok(names.into_iter().collect())
}

fn preview<'py>(client: &Any<'py>, request: &Any<'py>, state: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let database: Any<'py> = request.getattr("database")?;
    let retention_days: Any<'py> = request.getattr("retention_days")?;
    let minimum: Any<'py> = request.getattr("minimum_rollback_deployments")?;
    let inventory: Inventory<'py> = load_inventory(client, &request.getattr("metadata_database")?)?;
    let relations: HashSet<(String, String)> = if minimum.is_truthy()? {
        physical_relations(client, &inventory.deployments, &database)?
    } else {
        HashSet::new()
    };
    let datetime: Any<'py> = attr(py, "datetime", "datetime")?;
    let now: Any<'py> = datetime.call_method(
        "now",
        (),
        Some(&py_kwargs!(py; tz = attr(py, "datetime", "UTC")?)?),
    )?;
    let window: Any<'py> = attr(py, "datetime", "timedelta")?
        .call((), Some(&py_kwargs!(py; days = &retention_days)?))?;
    let cutoff: i64 = micros(&now.sub(window)?)?;
    let decisions: Vec<(usize, bool, String)> = janitor_preview(
        inventory.native_deployments,
        inventory.publications,
        bindings(&state.getattr("active_bindings")?)?,
        relations,
        (
            database.extract()?,
            retention_days.extract()?,
            cutoff,
            minimum.extract()?,
        ),
    );
    let live: HashSet<String> = client
        .call_method1("load_catalog", (&database,))?
        .call_method0("relation_names")?
        .try_iter()?
        .map(|name| name?.extract())
        .collect::<PyResult<_>>()?;
    let candidate_class: Any<'py> = attr(py, JANITOR_MODELS, "JanitorPreviewCandidate")?;
    let view_types: [Any<'py>; 2] = [
        attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_TABLE")?,
        attr(py, COMPILE_CONSTANTS, "DESIRED_OBJECT_TYPE_VIEW")?,
    ];
    let mut candidates: Vec<Any<'py>> = Vec::new();
    for (position, deletable, reason) in decisions {
        let deployment: &Any<'py> = &inventory.deployments[position];
        let mut views: Vec<String> = Vec::new();
        for mapping in items(deployment, "prepared_object_mappings")? {
            let key: Any<'py> = mapping.getattr("logical_key")?;
            let object_type: Any<'py> = key.getattr("object_type")?;
            if object_type.eq(&view_types[0])? || object_type.eq(&view_types[1])? {
                views.push(key.getattr("name")?.extract()?);
            }
        }
        views.sort();
        candidates.push(candidate_class.call(
            (),
            Some(&py_kwargs!(py;
                deployment_id = deployment.getattr("deployment_id")?,
                created_at = deployment.getattr("created_at")?,
                status = deployment.getattr("status")?,
                logical_view_names = PyTuple::new(py, views)?,
                physical_object_names = PyTuple::new(py, candidate_objects(deployment, &live)?)?,
                deletable = deletable,
                reason = reason,
            )?),
        )?);
    }
    attr(py, JANITOR_MODELS, "JanitorPreviewResult")?.call(
        (),
        Some(&py_kwargs!(py;
            database = &database,
            retention_days = &retention_days,
            minimum_rollback_deployments = &minimum,
            candidates = PyTuple::new(py, candidates)?,
        )?),
    )
}

fn apply<'py>(client: &Any<'py>, request: &Any<'py>, state: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let database: Any<'py> = request.getattr("database")?;
    let previewed: Any<'py> = preview(client, request, state)?;
    let inventory: Inventory<'py> = load_inventory(client, &request.getattr("metadata_database")?)?;
    let removals: Vec<(String, String)> = janitor_binding_activity(
        inventory.native_deployments,
        inventory.publications,
        bindings(&state.getattr("active_bindings")?)?,
    )
    .1;
    let removal_class: Any<'py> = attr(py, ADAPTER_MODELS, "AdapterStableBindingRemoval")?;
    let obsolete: Vec<Any<'py>> = removals
        .iter()
        .map(|(removal_database, logical_name)| {
            removal_class.call(
                (),
                Some(&py_kwargs!(py; database = removal_database, logical_name = logical_name)?),
            )
        })
        .collect::<PyResult<_>>()?;
    let binding_request: Any<'py> = attr(py, ADAPTER_MODELS, "AdapterBindingReplacementRequest")?
        .call(
            (),
            Some(&py_kwargs!(py; bindings = PyTuple::empty(py), removals = PyTuple::new(py, &obsolete)?)?),
        )?;
    let mut deleted_deployments: Vec<Any<'py>> = Vec::new();
    let mut requested: Vec<Any<'py>> = Vec::new();
    for candidate in items(&previewed, "candidates")? {
        if !candidate.getattr("deletable")?.is_truthy()? {
            continue;
        }
        requested.extend(items(&candidate, "physical_object_names")?);
        deleted_deployments.push(candidate.getattr("deployment_id")?);
    }
    let cleanup_request: Any<'py> = attr(py, ADAPTER_MODELS, "AdapterRelationCleanupRequest")?
        .call(
            (),
            Some(&py_kwargs!(py; database = &database, relation_names = PyTuple::new(py, &requested)?)?),
        )?;
    let refreshed: Any<'py> = client.call_method1("inspect_managed_table_state", (&database,))?;
    let obsolete_keys: HashSet<(String, String)> = removals.into_iter().collect();
    let mut active: HashSet<String> = HashSet::new();
    for binding in items(&refreshed, "active_bindings")? {
        let key: (String, String) = (
            binding.getattr("database")?.extract()?,
            binding.getattr("logical_name")?.extract()?,
        );
        if !obsolete_keys.contains(&key) {
            active.insert(binding.getattr("physical_name")?.extract()?);
        }
    }
    let relation_names: Any<'py> = cleanup_request.getattr("relation_names")?;
    let mut newly_active: Vec<String> = Vec::new();
    for name in relation_names.try_iter()? {
        let name: String = name?.extract()?;
        if active.contains(&name) && !newly_active.contains(&name) {
            newly_active.push(name);
        }
    }
    if !newly_active.is_empty() {
        newly_active.sort();
        let names: Bound<'py, PyTuple> = PyTuple::new(py, newly_active)?;
        return Err(PyErr::from_value(
            attr(py, ADAPTER_EXCEPTIONS, "AdapterResultError")?.call1((format!(
                "Refusing to clean relations that became active: {}",
                names.repr()?
            ),))?,
        ));
    }
    let binding_sql: Any<'py> =
        client.call_method1("render_replace_stable_bindings", (&binding_request,))?;
    let cleanup_sql: Any<'py> =
        client.call_method1("render_cleanup_relations", (&cleanup_request,))?;
    let mut statements: Vec<Any<'py>> =
        numbered(&binding_sql, 1, "remove_obsolete_binding", "TEARDOWN")?;
    let next: usize = statements.len() + 1;
    statements.extend(numbered(
        &cleanup_sql,
        next,
        "cleanup_relation",
        "TEARDOWN",
    )?);
    attr(py, WAREHOUSE_WORKFLOW, "execute_warehouse_workflow")?.call(
        (),
        Some(&py_kwargs!(py; statements = PyTuple::new(py, statements)?, connection = client)?),
    )?;
    attr(py, JANITOR_MODELS, "JanitorApplyResult")?.call(
        (),
        Some(&py_kwargs!(py;
            database = &database,
            retention_days = request.getattr("retention_days")?,
            minimum_rollback_deployments = request.getattr("minimum_rollback_deployments")?,
            deleted_deployment_ids = PyTuple::new(py, deleted_deployments)?,
            deleted_object_names = relation_names,
        )?),
    )
}

/// Preview or apply the janitor for one target, holding its mutation lock while applying.
#[pyfunction]
#[pyo3(signature = (*, request, client))]
fn execute_janitor<'py>(request: &Any<'py>, client: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = client.py();
    let database: Any<'py> = request.getattr("database")?;
    if !request.getattr("apply")?.is_truthy()? {
        let state: Any<'py> = client.call_method1("inspect_managed_table_state", (&database,))?;
        return preview(client, request, &state);
    }
    let lock: Any<'py> = attr(py, MUTATION_LOCK, "target_mutation_lock")?.call(
        (),
        Some(&py_kwargs!(py; connection = client, database = &database)?),
    )?;
    within(&lock, || {
        let state: Any<'py> = client.call_method1("inspect_managed_table_state", (&database,))?;
        apply(client, request, &state)
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execute_janitor, module)?)
}
