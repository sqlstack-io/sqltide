//! ClickHouse deployment lifecycle: managed-table inspection from system tables, the deployment
//! and publication inventory from metadata tables, relation cleanup and stable-binding
//! replacement statements. Warehouse reads go through the Python adapter connection.

use std::collections::BTreeSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_planner::inspection::deployment_candidate_logical_name as candidate_logical_name;
use sqltide_sqltext::python_text::python_isspace;

use crate::clickhouse_catalog::{
    analyze, build, field, object, query_mappings, result_error, strip, text,
};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const VIEW_ENGINE: &str = "View";
const DEPLOYMENTS_TABLE: &str = "_sqltide_virtual_deployments";
const OBJECT_STATE_TABLE: &str = "_sqltide_virtual_object_state";
const PUBLISH_HISTORY_TABLE: &str = "_sqltide_virtual_publications";
const PROGRESS_TABLE: &str = "_sqltide_virtual_deployment_progress";
const STAGE_STARTED: &str = "started";
const STAGE_COMPLETED: &str = "completed";
const PUBLISH_OPERATION_COLUMN: &str = "operation";
const PUBLISH_PREVIOUS_DEPLOYMENT_COLUMN: &str = "previous_deployment_id";
const DEPLOYMENT_STATUS_INCOMPLETE: &str = "incomplete";

/// The stable-view target of one view query, when it has the direct stable-binding shape.
pub(crate) fn stable_binding(
    py: Python<'_>,
    engine: &str,
    as_select: &str,
) -> PyResult<Option<String>> {
    if engine != VIEW_ENGINE || strip(as_select).is_empty() {
        return Ok(None);
    }
    Ok(analyze(py, as_select)?
        .direct_source
        .map(|source| source.name))
}

/// Managed-table inspection from ClickHouse system metadata.
#[pyfunction]
#[pyo3(signature = (*, client, database))]
fn build_inspected_managed_table_state<'py>(
    py: Python<'py>,
    client: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let view_rows: Vec<(String, String)> = query_mappings(
        client,
        format!("SELECT name, as_select FROM system.tables WHERE database = '{database}' AND engine = 'View'"),
    )?
    .iter()
    .map(|row| Ok((field(row, "name")?, field(row, "as_select")?)))
    .collect::<PyResult<_>>()?;
    let table_names: Vec<String> = query_mappings(
        client,
        format!(
            "SELECT name FROM system.tables WHERE database = '{database}' AND engine != 'View'"
        ),
    )?
    .iter()
    .map(|row| field(row, "name"))
    .collect::<PyResult<_>>()?;
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    let candidate_class: Bound<'py, PyAny> = models.getattr("InspectedPhysicalTableCandidate")?;
    let binding_class: Bound<'py, PyAny> = models.getattr("InspectedActiveTableBinding")?;
    let candidate =
        |logical: &str, physical: &str, object_type: &str| -> PyResult<Bound<'py, PyAny>> {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("database", database)?;
            keywords.set_item("logical_name", logical)?;
            keywords.set_item("physical_name", physical)?;
            keywords.set_item("object_type", object_type)?;
            candidate_class.call((), Some(&keywords))
        };
    let mut bindings: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut candidates: Vec<Bound<'py, PyAny>> = Vec::new();
    for (name, as_select) in &view_rows {
        if let Some(logical) = candidate_logical_name(name) {
            candidates.push(candidate(logical, name, "view")?);
            continue;
        }
        let Some(physical) = stable_binding(py, VIEW_ENGINE, as_select)? else {
            continue;
        };
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("database", database)?;
        keywords.set_item("logical_name", name)?;
        keywords.set_item("physical_name", physical)?;
        bindings.push(binding_class.call((), Some(&keywords))?);
    }
    for name in &table_names {
        if let Some(logical) = candidate_logical_name(name) {
            candidates.push(candidate(logical, name, "table")?);
        }
    }
    build(
        py,
        ADAPTER_MODELS,
        "InspectedManagedTableState",
        &[
            ("active_bindings", PyTuple::new(py, bindings)?.into_any()),
            (
                "physical_candidates",
                PyTuple::new(py, candidates)?.into_any(),
            ),
        ],
    )
}

struct DeploymentRow {
    deployment_id: String,
    created_at: String,
    replay_lineage_mode: String,
    header_present: bool,
}

struct ObjectRow {
    deployment_id: String,
    database_name: Option<String>,
    object_type: String,
    object_name: String,
    physical_name: Option<String>,
    logical_model_name: Option<String>,
    is_selected_root: bool,
    observed_at: String,
}

struct PublishRow {
    publication_id: String,
    deployment_id: String,
    published_at: String,
    database_name: String,
    logical_view_name: String,
    physical_relation_name: String,
    operation: String,
    previous_deployment_id: Option<String>,
}

fn optional_field(row: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<String>> {
    let value: Bound<'_, PyAny> = row.get_item(name)?;
    if value.is_none() {
        Ok(None)
    } else {
        text(&value).map(Some)
    }
}

fn is_missing_relation(py: Python<'_>, failure: &PyErr) -> PyResult<bool> {
    failure.value(py).is_instance(
        &py.import(ADAPTER_EXCEPTIONS)?
            .getattr("AdapterRelationNotFoundError")?,
    )
}

/// Rows of one metadata query, or none when its metadata table does not exist yet.
fn metadata_rows<'py, T>(
    connection: &Bound<'py, PyAny>,
    load: impl FnOnce() -> PyResult<Vec<Bound<'py, PyAny>>>,
    decode: impl Fn(&Bound<'py, PyAny>) -> PyResult<T>,
) -> PyResult<Vec<T>> {
    let py: Python<'py> = connection.py();
    match load() {
        Ok(rows) => rows.iter().map(decode).collect(),
        Err(failure) if is_missing_relation(py, &failure)? => Ok(Vec::new()),
        Err(failure) => Err(failure),
    }
}

fn publish_rows<'py>(connection: &Bound<'py, PyAny>, database: &str) -> PyResult<Vec<PublishRow>> {
    let py: Python<'py> = connection.py();
    metadata_rows(
        connection,
        || {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("database", database)?;
            keywords.set_item("table", PUBLISH_HISTORY_TABLE)?;
            let columns: Bound<'py, PyAny> = connection
                .getattr("metadata_columns")?
                .call((), Some(&keywords))?;
            let operation: &str = if columns.contains(PUBLISH_OPERATION_COLUMN)? {
                "operation"
            } else {
                "'promote' AS operation"
            };
            let previous: &str = if columns.contains(PUBLISH_PREVIOUS_DEPLOYMENT_COLUMN)? {
                "previous_deployment_id"
            } else {
                "CAST(NULL AS Nullable(String)) AS previous_deployment_id"
            };
            query_mappings(
                connection,
                format!(
                    "SELECT DISTINCT publication_id, deployment_id, {operation}, {previous}, \
                     published_at, logical_database_name, logical_view_name, \
                     physical_relation_name FROM {database}.{PUBLISH_HISTORY_TABLE}"
                ),
            )
        },
        |row| {
            Ok(PublishRow {
                publication_id: field(row, "publication_id")?,
                deployment_id: field(row, "deployment_id")?,
                published_at: field(row, "published_at")?,
                database_name: field(row, "logical_database_name")?,
                logical_view_name: field(row, "logical_view_name")?,
                physical_relation_name: field(row, "physical_relation_name")?,
                operation: field(row, "operation")?,
                previous_deployment_id: optional_field(row, "previous_deployment_id")?,
            })
        },
    )
}

fn metadata_key<'py>(py: Python<'py>, row: &ObjectRow) -> PyResult<Bound<'py, PyAny>> {
    build(
        py,
        ADAPTER_MODELS,
        "AdapterMetadataObjectKey",
        &[
            ("database", object(py, &row.database_name)?),
            ("object_type", object(py, &row.object_type)?),
            ("name", object(py, &row.object_name)?),
        ],
    )
}

fn deployment_record<'py>(
    py: Python<'py>,
    row: &DeploymentRow,
    objects: &[ObjectRow],
    publications: &[PublishRow],
    unfinished: &BTreeSet<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let owned: Vec<&ObjectRow> = objects
        .iter()
        .filter(|object_row| object_row.deployment_id == row.deployment_id)
        .collect();
    let status: &str = if !row.header_present || unfinished.contains(&row.deployment_id) {
        DEPLOYMENT_STATUS_INCOMPLETE
    } else if publications
        .iter()
        .any(|event| event.deployment_id == row.deployment_id)
    {
        "published"
    } else {
        "staged"
    };
    let roots: Vec<Bound<'py, PyAny>> = owned
        .iter()
        .filter(|object_row| object_row.is_selected_root)
        .map(|object_row| metadata_key(py, object_row))
        .collect::<PyResult<_>>()?;
    let mapping_class: Bound<'py, PyAny> = py
        .import(ADAPTER_MODELS)?
        .getattr("AdapterPreparedObjectMapping")?;
    let mut mappings: Vec<Bound<'py, PyAny>> = Vec::new();
    for object_row in &owned {
        let (Some(physical), Some(model)) =
            (&object_row.physical_name, &object_row.logical_model_name)
        else {
            continue;
        };
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("logical_key", metadata_key(py, object_row)?)?;
        keywords.set_item("physical_name", physical)?;
        keywords.set_item("logical_model_name", model)?;
        mappings.push(mapping_class.call((), Some(&keywords))?);
    }
    build(
        py,
        ADAPTER_MODELS,
        "AdapterDeploymentRecord",
        &[
            ("deployment_id", object(py, &row.deployment_id)?),
            ("created_at", object(py, &row.created_at)?),
            ("status", object(py, status)?),
            ("replay_lineage_mode", object(py, &row.replay_lineage_mode)?),
            ("selected_root_keys", PyTuple::new(py, roots)?.into_any()),
            ("warning_codes", PyTuple::empty(py).into_any()),
            (
                "prepared_object_mappings",
                PyTuple::new(py, mappings)?.into_any(),
            ),
        ],
    )
}

fn publish_event<'py>(py: Python<'py>, rows: &[&PublishRow]) -> PyResult<Bound<'py, PyAny>> {
    let first: &PublishRow = rows[0];
    let binding_class: Bound<'py, PyAny> =
        py.import(ADAPTER_MODELS)?.getattr("AdapterStableBinding")?;
    let bindings: Vec<Bound<'py, PyAny>> = rows
        .iter()
        .map(|row| {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("database", &row.database_name)?;
            keywords.set_item("logical_name", &row.logical_view_name)?;
            keywords.set_item("physical_name", &row.physical_relation_name)?;
            binding_class.call((), Some(&keywords))
        })
        .collect::<PyResult<_>>()?;
    let names: Vec<&str> = rows
        .iter()
        .map(|row| row.logical_view_name.as_str())
        .collect();
    build(
        py,
        ADAPTER_MODELS,
        "AdapterPublishEventRecord",
        &[
            ("deployment_id", object(py, &first.deployment_id)?),
            ("published_at", object(py, &first.published_at)?),
            ("logical_view_names", PyTuple::new(py, names)?.into_any()),
            ("publication_id", object(py, &first.publication_id)?),
            ("bindings", PyTuple::new(py, bindings)?.into_any()),
            ("operation", object(py, &first.operation)?),
            (
                "previous_deployment_id",
                object(py, &first.previous_deployment_id)?,
            ),
        ],
    )
}

/// Neutral deployment and publish-event records from ClickHouse metadata.
#[pyfunction]
#[pyo3(signature = (*, connection, database))]
fn load_clickhouse_deployment_inventory<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let mut deployments: Vec<DeploymentRow> = metadata_rows(
        connection,
        || {
            query_mappings(
                connection,
                format!(
                    "SELECT deployment_id, created_at, replay_lineage_mode \
                     FROM {database}.{DEPLOYMENTS_TABLE}"
                ),
            )
        },
        |row| {
            Ok(DeploymentRow {
                deployment_id: field(row, "deployment_id")?,
                created_at: field(row, "created_at")?,
                replay_lineage_mode: field(row, "replay_lineage_mode")?,
                header_present: true,
            })
        },
    )?;
    let objects: Vec<ObjectRow> = metadata_rows(
        connection,
        || {
            query_mappings(
                connection,
                format!(
                    "SELECT DISTINCT deployment_id, logical_database_name, logical_object_type, \
                     logical_object_name, physical_relation_name, logical_model_name, \
                     is_selected_root , observed_at FROM {database}.{OBJECT_STATE_TABLE} \
                     WHERE state_kind = 'deployment' AND deployment_id IS NOT NULL"
                ),
            )
        },
        |row| {
            Ok(ObjectRow {
                deployment_id: field(row, "deployment_id")?,
                database_name: optional_field(row, "logical_database_name")?,
                object_type: field(row, "logical_object_type")?,
                object_name: field(row, "logical_object_name")?,
                physical_name: optional_field(row, "physical_relation_name")?,
                logical_model_name: optional_field(row, "logical_model_name")?,
                is_selected_root: row.get_item("is_selected_root")?.is_truthy()?,
                observed_at: field(row, "observed_at")?,
            })
        },
    )?;
    let publications: Vec<PublishRow> = publish_rows(connection, database)?;
    // Builds that recorded their start but not their completion failed while filling their
    // tables. Deployments from before this record existed have neither, and stay complete.
    let progress: Vec<(String, String)> = metadata_rows(
        connection,
        || {
            query_mappings(
                connection,
                format!("SELECT DISTINCT deployment_id, stage FROM {database}.{PROGRESS_TABLE}"),
            )
        },
        |row| Ok((field(row, "deployment_id")?, field(row, "stage")?)),
    )?;
    let completed: BTreeSet<&str> = progress
        .iter()
        .filter(|(_, stage)| stage == STAGE_COMPLETED)
        .map(|(deployment_id, _)| deployment_id.as_str())
        .collect();
    let unfinished: BTreeSet<String> = progress
        .iter()
        .filter(|(deployment_id, stage)| {
            stage == STAGE_STARTED && !completed.contains(deployment_id.as_str())
        })
        .map(|(deployment_id, _)| deployment_id.clone())
        .collect();
    let persisted: BTreeSet<&str> = deployments
        .iter()
        .map(|row| row.deployment_id.as_str())
        .collect();
    let headerless: BTreeSet<String> = objects
        .iter()
        .filter(|row| !persisted.contains(row.deployment_id.as_str()))
        .map(|row| row.deployment_id.clone())
        .collect();
    for deployment_id in headerless {
        let created_at: String = objects
            .iter()
            .filter(|row| row.deployment_id == deployment_id)
            .map(|row| row.observed_at.clone())
            .min()
            .unwrap_or_default();
        deployments.push(DeploymentRow {
            deployment_id,
            created_at,
            replay_lineage_mode: "unknown".to_owned(),
            header_present: false,
        });
    }
    let records: Vec<Bound<'py, PyAny>> = deployments
        .iter()
        .map(|row| deployment_record(py, row, &objects, &publications, &unfinished))
        .collect::<PyResult<_>>()?;
    let publication_ids: BTreeSet<&str> = publications
        .iter()
        .map(|row| row.publication_id.as_str())
        .collect();
    let events: Vec<Bound<'py, PyAny>> = publication_ids
        .iter()
        .map(|publication_id| {
            let rows: Vec<&PublishRow> = publications
                .iter()
                .filter(|row| row.publication_id == *publication_id)
                .collect();
            publish_event(py, &rows)
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterDeploymentInventory",
        &[
            ("deployments", PyTuple::new(py, records)?.into_any()),
            ("publish_events", PyTuple::new(py, events)?.into_any()),
        ],
    )
}

/// Synchronous drops of the requested relations, each refused while a stable view binds it.
#[pyfunction]
#[pyo3(signature = (*, connection, request))]
fn render_clickhouse_relation_cleanup<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let database: Bound<'py, PyAny> = request.getattr("database")?;
    let database_name: String = text(&database)?;
    let catalog: Bound<'py, PyAny> = connection.call_method1("load_catalog", (&database,))?;
    let mut statements: Vec<String> = Vec::new();
    for relation_name in request.getattr("relation_names")?.try_iter()? {
        let relation_name: Bound<'py, PyAny> = relation_name?;
        let state: Bound<'py, PyAny> =
            connection.call_method1("inspect_managed_table_state", (&database,))?;
        for binding in state.getattr("active_bindings")?.try_iter()? {
            if binding?.getattr("physical_name")?.eq(&relation_name)? {
                return Err(result_error(
                    py,
                    format!(
                        "Refusing to clean active physical relation '{}'",
                        text(&relation_name)?
                    ),
                )?);
            }
        }
        let relation: Bound<'py, PyAny> = catalog.call_method1("relation", (&relation_name,))?;
        let kind: &str = if !relation.is_none() && relation.getattr("engine")?.eq(VIEW_ENGINE)? {
            "VIEW"
        } else {
            "TABLE"
        };
        statements.push(format!(
            "DROP {kind} IF EXISTS {database_name}.{} SYNC;",
            text(&relation_name)?
        ));
    }
    PyTuple::new(py, statements)
}

/// `statement` ending in exactly one semicolon.
fn terminated(statement: &str) -> String {
    let trimmed: &str = statement
        .trim_end_matches(python_isspace)
        .trim_end_matches(';');
    format!("{trimmed};")
}

/// Exact stable-binding replacements followed by the requested binding removals.
#[pyfunction]
#[pyo3(signature = (*, connection, request))]
fn render_clickhouse_stable_binding_replacement<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let view_class: Bound<'py, PyAny> = py.import(ADAPTER_MODELS)?.getattr("AdapterStableView")?;
    let mut statements: Vec<String> = Vec::new();
    for binding in request.getattr("bindings")?.try_iter()? {
        let binding: Bound<'py, PyAny> = binding?;
        let view_keywords: Bound<'py, PyDict> = PyDict::new(py);
        view_keywords.set_item("name", binding.getattr("logical_name")?)?;
        view_keywords.set_item("target_relation_name", binding.getattr("physical_name")?)?;
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("database", binding.getattr("database")?)?;
        keywords.set_item("resource", view_class.call((), Some(&view_keywords))?)?;
        let rendered: String = connection
            .getattr("render_resource")?
            .call((), Some(&keywords))?
            .extract()?;
        statements.push(terminated(&rendered));
    }
    for removal in request.getattr("removals")?.try_iter()? {
        let removal: Bound<'py, PyAny> = removal?;
        statements.push(format!(
            "DROP VIEW IF EXISTS {}.{} SYNC;",
            text(&removal.getattr("database")?)?,
            text(&removal.getattr("logical_name")?)?
        ));
    }
    PyTuple::new(py, statements)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        build_inspected_managed_table_state,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        load_clickhouse_deployment_inventory,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        render_clickhouse_relation_cleanup,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        render_clickhouse_stable_binding_replacement,
        module
    )?)
}
