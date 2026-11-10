//! Optional ClickHouse metadata reads: the latest direct SQL baselines and the manifest history
//! of one project target. A warehouse failure leaves the state unavailable with a warning;
//! an incompatible table schema does too. Warehouse reads go through the Python adapter
//! connection.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyTuple};
use sqltide_clickhouse::metadata::{latest_fingerprints_query, manifest_history_query};

use crate::clickhouse_catalog::{build, object, result_error, text};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const DIRECT_FINGERPRINTS_TABLE: &str = "_sqltide_direct_fingerprints";
const MANIFESTS_TABLE: &str = "_sqltide_manifests";
const DIRECT_FINGERPRINT_COLUMNS: [&str; 8] = [
    "fingerprint_id",
    "logical_model_identity",
    "definition_sql",
    "definition_hash",
    "identity_metadata",
    "workflow_id",
    "tool_version",
    "applied_at",
];
const MANIFEST_COLUMNS: [&str; 13] = [
    "manifest_id",
    "invocation_id",
    "project_identity",
    "target_name",
    "target_database",
    "is_production",
    "project_revision",
    "manifest_fingerprint",
    "manifest_version",
    "pipelines",
    "resources",
    "tool_version",
    "published_at",
];
const MANIFEST_RESOURCE_FIELDS: [&str; 7] = [
    "pipeline_name",
    "logical_type",
    "logical_name",
    "resource_role",
    "resource_database",
    "resource_name",
    "resource_kind",
];

/// What one optional metadata table offers: absent, incompatible, or readable rows.
enum TableRead<'py> {
    Absent,
    Incompatible,
    Rows(Vec<Bound<'py, PyAny>>),
    Failed(Bound<'py, PyAny>),
}

/// Reads one optional metadata table after checking its schema; a warehouse error becomes
/// `Failed`, and `query` returning `None` means there is nothing to read.
fn read_table<'py>(
    connection: &Bound<'py, PyAny>,
    database: &str,
    table: &str,
    required: &[&str],
    query: impl FnOnce() -> Option<String>,
) -> PyResult<TableRead<'py>> {
    let py: Python<'py> = connection.py();
    let attempt = || -> PyResult<TableRead<'py>> {
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("database", database)?;
        keywords.set_item("table", table)?;
        let columns: Bound<'py, PyAny> = connection
            .getattr("metadata_columns")?
            .call((), Some(&keywords))?;
        if !columns.is_truthy()? {
            return Ok(TableRead::Absent);
        }
        if !PyFrozenSet::new(py, required)?.le(&columns)? {
            return Ok(TableRead::Incompatible);
        }
        let Some(statement) = query() else {
            return Ok(TableRead::Rows(Vec::new()));
        };
        let result: Bound<'py, PyAny> = connection.call_method1("query", (statement,))?;
        Ok(TableRead::Rows(
            result
                .getattr("rows")?
                .try_iter()?
                .collect::<PyResult<_>>()?,
        ))
    };
    match attempt() {
        Ok(read) => Ok(read),
        Err(failure)
            if failure.value(py).is_instance(
                &py.import(ADAPTER_EXCEPTIONS)?
                    .getattr("AdapterWarehouseError")?,
            )? =>
        {
            Ok(TableRead::Failed(failure.value(py).clone().into_any()))
        }
        Err(failure) => Err(failure),
    }
}

fn status<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(ADAPTER_TYPES)?
        .getattr("AdapterOptionalStateStatus")?
        .getattr(name)
}

/// Latest compatible direct SQL baselines, without making them execution state.
#[pyfunction]
#[pyo3(signature = (*, connection, database, logical_model_identities))]
fn load_clickhouse_direct_fingerprints<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
    logical_model_identities: Vec<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let snapshot = |state: &str,
                    baselines: Vec<Bound<'py, PyAny>>,
                    warning: Option<String>|
     -> PyResult<Bound<'py, PyAny>> {
        build(
            py,
            ADAPTER_MODELS,
            "AdapterDirectFingerprintSnapshot",
            &[
                ("status", status(py, state)?),
                ("baselines", PyTuple::new(py, baselines)?.into_any()),
                ("warning", object(py, warning)?),
            ],
        )
    };
    let read: TableRead<'py> = read_table(
        connection,
        database,
        DIRECT_FINGERPRINTS_TABLE,
        &DIRECT_FINGERPRINT_COLUMNS,
        || {
            (!logical_model_identities.is_empty())
                .then(|| latest_fingerprints_query(database, &logical_model_identities))
        },
    )?;
    match read {
        TableRead::Absent => snapshot("ABSENT", Vec::new(), None),
        TableRead::Incompatible => snapshot(
            "UNAVAILABLE",
            Vec::new(),
            Some("Direct SQL baseline table has an incompatible schema".to_owned()),
        ),
        TableRead::Failed(error) => snapshot(
            "UNAVAILABLE",
            Vec::new(),
            Some(format!(
                "Direct SQL baselines unavailable: {}",
                error.str()?
            )),
        ),
        TableRead::Rows(rows) => {
            let record_class: Bound<'py, PyAny> = py
                .import(ADAPTER_MODELS)?
                .getattr("AdapterDirectFingerprintRecord")?;
            let baselines: Vec<Bound<'py, PyAny>> = rows
                .iter()
                .map(|row| {
                    let keywords: Bound<'py, PyDict> = PyDict::new(py);
                    for (index, name) in DIRECT_FINGERPRINT_COLUMNS.iter().enumerate() {
                        keywords.set_item(name, text(&row.get_item(index)?)?)?;
                    }
                    record_class.call((), Some(&keywords))
                })
                .collect::<PyResult<_>>()?;
            snapshot("AVAILABLE", baselines, None)
        }
    }
}

fn manifest_resource<'py>(resource: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = resource.py();
    let abc: Bound<'py, PyModule> = py.import("collections.abc")?;
    let values: Vec<Bound<'py, PyAny>> = if resource.is_instance(&abc.getattr("Mapping")?)? {
        MANIFEST_RESOURCE_FIELDS
            .iter()
            .map(|name| resource.get_item(name))
            .collect::<PyResult<_>>()?
    } else if resource.is_instance(&abc.getattr("Sequence")?)?
        && !resource.is_instance_of::<pyo3::types::PyString>()
        && !resource.is_instance_of::<pyo3::types::PyBytes>()
    {
        resource.try_iter()?.collect::<PyResult<_>>()?
    } else {
        return Err(result_error(
            py,
            "Manifest resource row is not a tuple".to_owned(),
        )?);
    };
    if values.len() != MANIFEST_RESOURCE_FIELDS.len() {
        return Err(result_error(
            py,
            "Manifest resource row has an incompatible shape".to_owned(),
        )?);
    }
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in MANIFEST_RESOURCE_FIELDS.iter().zip(&values) {
        keywords.set_item(name, text(value)?)?;
    }
    py.import(ADAPTER_MODELS)?
        .getattr("AdapterManifestResource")?
        .call((), Some(&keywords))
}

fn manifest<'py>(row: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = row.py();
    let item = |index: usize| row.get_item(index);
    let pipelines: Vec<String> = item(9)?
        .try_iter()?
        .map(|pipeline| text(&pipeline?))
        .collect::<PyResult<_>>()?;
    let resources: Vec<Bound<'py, PyAny>> = item(10)?
        .try_iter()?
        .map(|resource| manifest_resource(&resource?))
        .collect::<PyResult<_>>()?;
    let revision: Bound<'py, PyAny> = item(6)?;
    let revision: Option<String> = if revision.is_none() {
        None
    } else {
        Some(text(&revision)?)
    };
    let int: Bound<'py, PyAny> = py.import("builtins")?.getattr("int")?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterManifest",
        &[
            ("manifest_id", object(py, text(&item(0)?)?)?),
            ("invocation_id", object(py, text(&item(1)?)?)?),
            ("project_identity", object(py, text(&item(2)?)?)?),
            ("target_name", object(py, text(&item(3)?)?)?),
            ("target_database", object(py, text(&item(4)?)?)?),
            ("is_production", object(py, item(5)?.is_truthy()?)?),
            ("project_revision", object(py, revision)?),
            ("manifest_fingerprint", object(py, text(&item(7)?)?)?),
            ("manifest_version", int.call1((item(8)?,))?),
            ("pipelines", PyTuple::new(py, pipelines)?.into_any()),
            ("resources", PyTuple::new(py, resources)?.into_any()),
            ("tool_version", object(py, text(&item(11)?)?)?),
            ("published_at", object(py, text(&item(12)?)?)?),
        ],
    )
}

/// Complete manifest history for one project target, newest first.
#[pyfunction]
#[pyo3(signature = (*, connection, database, project_identity, target_name, target_database))]
fn load_clickhouse_manifests<'py>(
    py: Python<'py>,
    connection: &Bound<'py, PyAny>,
    database: &str,
    project_identity: &str,
    target_name: &str,
    target_database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let snapshot = |state: &str,
                    manifests: Vec<Bound<'py, PyAny>>,
                    warning: Option<String>|
     -> PyResult<Bound<'py, PyAny>> {
        build(
            py,
            ADAPTER_MODELS,
            "AdapterManifestSnapshot",
            &[
                ("status", object(py, state)?),
                ("manifests", PyTuple::new(py, manifests)?.into_any()),
                ("warning", object(py, warning)?),
            ],
        )
    };
    let read: TableRead<'py> = read_table(
        connection,
        database,
        MANIFESTS_TABLE,
        &MANIFEST_COLUMNS,
        || {
            Some(manifest_history_query(
                database,
                project_identity,
                target_name,
                target_database,
            ))
        },
    )?;
    match read {
        TableRead::Absent => snapshot("absent", Vec::new(), None),
        TableRead::Incompatible => snapshot(
            "unavailable",
            Vec::new(),
            Some("Manifest table has an incompatible schema".to_owned()),
        ),
        TableRead::Failed(error) => snapshot(
            "unavailable",
            Vec::new(),
            Some(format!("Manifest history unavailable: {}", error.str()?)),
        ),
        TableRead::Rows(rows) => {
            let manifests: Vec<Bound<'py, PyAny>> =
                rows.iter().map(manifest).collect::<PyResult<_>>()?;
            snapshot("available", manifests, None)
        }
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        load_clickhouse_direct_fingerprints,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(load_clickhouse_manifests, module)?)
}
