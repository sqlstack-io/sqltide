//! ClickHouse SQL rendering for the Python adapter: reads the neutral adapter dataclasses and
//! returns SQL text.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use sqltide_clickhouse::CapabilityError;
use sqltide_clickhouse::comparison::{SetDifferenceTarget, render_set_difference_comparison};
use sqltide_clickhouse::resources::{
    Column, ManagedSource, MaterializedView, Resource, StableView, Table, View,
    database_scoped_consumer_group as scoped_consumer_group, ensure_database, render_resource,
};

const ADAPTER_EXCEPTIONS_MODULE: &str = "sqltide.adapter.exceptions";

fn capability_error(py: Python<'_>, error: CapabilityError) -> PyErr {
    match py
        .import(ADAPTER_EXCEPTIONS_MODULE)
        .and_then(|module| module.getattr("AdapterCapabilityError"))
        .and_then(|class| class.call1((error.0,)))
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(failure) => failure,
    }
}

fn field<'py, T: FromPyObject<'py>>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<T> {
    object.getattr(name)?.extract()
}

fn columns(object: &Bound<'_, PyAny>) -> PyResult<Vec<Column>> {
    object
        .getattr("columns")?
        .try_iter()?
        .map(|column| {
            let column = column?;
            Ok(Column {
                name: field(&column, "name")?,
                type_sql: field(&column, "type")?,
                default_expression: field(&column, "default_expression")?,
            })
        })
        .collect()
}

fn resource(object: &Bound<'_, PyAny>) -> PyResult<Resource> {
    let class_name: String = object.get_type().name()?.extract()?;
    Ok(match class_name.as_str() {
        "AdapterManagedSource" => Resource::ManagedSource(ManagedSource {
            source_kind: field(object, "source_kind")?,
            name: field(object, "name")?,
            columns: columns(object)?,
            broker_list: field(object, "broker_list")?,
            topic: field(object, "topic")?,
            consumer_group: field(object, "consumer_group")?,
            format: field(object, "format")?,
            settings: field(object, "settings")?,
        }),
        "AdapterTable" => Resource::Table(Table {
            name: field(object, "name")?,
            columns: columns(object)?,
            engine: field(object, "engine")?,
            order_by: field(object, "order_by")?,
            partition_by: field(object, "partition_by")?,
            ttl: field(object, "ttl")?,
            settings: field(object, "settings")?,
        }),
        "AdapterMaterializedView" => Resource::MaterializedView(MaterializedView {
            name: field(object, "name")?,
            target_relation_name: field(object, "target_relation_name")?,
            database_template: field(object, "database_template")?,
            refresh: field(object, "refresh")?,
            append: field(object, "append")?,
        }),
        "AdapterView" => Resource::View(View {
            name: field(object, "name")?,
            database_template: field(object, "database_template")?,
        }),
        "AdapterStableView" => Resource::StableView(StableView {
            name: field(object, "name")?,
            target_relation_name: field(object, "target_relation_name")?,
        }),
        other => {
            return Err(PyTypeError::new_err(format!(
                "ClickHouse cannot render adapter resource '{other}'"
            )));
        }
    })
}

/// Render one neutral adapter resource as ClickHouse DDL, leaving credential placeholders.
#[pyfunction]
fn render_clickhouse_resource(
    py: Python<'_>,
    resource_object: &Bound<'_, PyAny>,
    database: &str,
    if_not_exists: bool,
) -> PyResult<String> {
    render_resource(&resource(resource_object)?, database, if_not_exists)
        .map_err(|error| capability_error(py, error))
}

/// Render bidirectional bag comparisons for every `AdapterSetDifferenceTarget`.
#[pyfunction]
fn render_clickhouse_set_difference_comparison(
    py: Python<'_>,
    targets: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let targets: Vec<SetDifferenceTarget> = targets
        .try_iter()?
        .map(|target| {
            let target = target?;
            Ok(SetDifferenceTarget {
                column_names: field(&target, "column_names")?,
                ctes: field(&target, "ctes")?,
                actual_query: field(&target, "actual_query")?,
                expected_query: field(&target, "expected_query")?,
            })
        })
        .collect::<PyResult<_>>()?;
    render_set_difference_comparison(&targets).map_err(|error| capability_error(py, error))
}

/// One manually executable ClickHouse database creation statement.
#[pyfunction]
fn render_clickhouse_ensure_database(database: &str) -> String {
    ensure_database(database)
}

/// The Kafka consumer group ClickHouse uses for one target database.
#[pyfunction]
fn database_scoped_consumer_group(consumer_group: &str, database: &str) -> String {
    scoped_consumer_group(consumer_group, database)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(render_clickhouse_resource, module)?)?;
    module.add_function(wrap_pyfunction!(
        render_clickhouse_set_difference_comparison,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(render_clickhouse_ensure_database, module)?)?;
    module.add_function(wrap_pyfunction!(database_scoped_consumer_group, module)?)
}
