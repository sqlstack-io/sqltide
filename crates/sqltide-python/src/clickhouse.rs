//! ClickHouse SQL rendering for the Python adapter: reads the neutral adapter dataclasses and
//! returns SQL text.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use sqltide_clickhouse::CapabilityError;
use sqltide_clickhouse::comparison::{SetDifferenceTarget, render_set_difference_comparison};
use sqltide_clickhouse::replay::{
    Boundary, BoundaryMode, Columns, DeploymentReplay, LowerBound, LowerBoundMode, Relations,
    Replay, ReplayError, Window, coverage_query, replay_from_capture, replay_from_deployment,
};
use sqltide_clickhouse::resources::{
    Column, ManagedSource, MaterializedView, Resource, StableView, Table, View,
    database_scoped_consumer_group as scoped_consumer_group, ensure_database, render_resource,
};

const ADAPTER_EXCEPTIONS_MODULE: &str = "sqltide.adapter.exceptions";

fn adapter_error(py: Python<'_>, class: &str, message: String) -> PyErr {
    match py
        .import(ADAPTER_EXCEPTIONS_MODULE)
        .and_then(|module| module.getattr(class))
        .and_then(|class| class.call1((message,)))
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(failure) => failure,
    }
}

fn capability_error(py: Python<'_>, error: CapabilityError) -> PyErr {
    adapter_error(py, "AdapterCapabilityError", error.0)
}

fn replay_error(py: Python<'_>, error: ReplayError) -> PyErr {
    adapter_error(py, "AdapterReplayError", error.0)
}

fn mode<T>(object: &Bound<'_, PyAny>, name: &str, parse: fn(&str) -> Option<T>) -> PyResult<T> {
    let value: String = object.getattr(name)?.str()?.extract()?;
    parse(&value).ok_or_else(|| PyTypeError::new_err(format!("Unknown replay {name} '{value}'")))
}

fn replay(object: &Bound<'_, PyAny>) -> PyResult<Replay> {
    let relations = object.getattr("relations")?;
    let replay_query = object.getattr("replay_query")?;
    let columns = object.getattr("columns")?;
    let window = object.getattr("window")?;
    let seed_mode: String = object.getattr("seed_mode")?.str()?.extract()?;
    Ok(Replay {
        mode: mode(object, "mode", BoundaryMode::parse)?,
        database: field(object, "database")?,
        relations: Relations {
            root: field(&relations, "root")?,
            source: field(&relations, "source")?,
            anchor: field(&relations, "anchor")?,
            target: field(&relations, "target")?,
        },
        query: field(&replay_query, "query")?,
        aggregate_semantics: field(&replay_query, "aggregate_semantics")?,
        boundaries: object
            .getattr("boundaries")?
            .try_iter()?
            .map(|boundary| {
                let boundary = boundary?;
                Ok(Boundary {
                    boundary_key: field(&boundary, "boundary_key")?,
                    cutoff_value: field(&boundary, "cutoff_value")?,
                    cutoff_inclusive: field(&boundary, "cutoff_inclusive")?,
                    partition_value: field(&boundary, "partition_value")?,
                })
            })
            .collect::<PyResult<_>>()?,
        columns: Columns {
            partition: field(&columns, "partition")?,
            offset: field(&columns, "offset")?,
            timestamp: field(&columns, "timestamp")?,
            landed_at: field(&columns, "landed_at")?,
            cursor: field(&columns, "cursor")?,
        },
        window: Window {
            lower_bound_mode: mode(&window, "lower_bound_mode", LowerBoundMode::parse)?,
            lower_bound_inclusive: field(&window, "lower_bound_inclusive")?,
            forced_start_time: field(&window, "forced_start_time")?,
            lookback_seconds: field(&window, "lookback_seconds")?,
        },
        seeds_history_prefix: seed_mode == "history_prefix",
        target_column_names: field(object, "target_column_names")?,
        settings: field(object, "settings")?,
        filter_boundaries_at_source: field(object, "filter_boundaries_at_source")?,
    })
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

/// Checkpoint coverage for one `AdapterReplayCoverageRequest`.
#[pyfunction]
fn render_clickhouse_replay_coverage_query(
    py: Python<'_>,
    request: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let boundary_column_type: Option<String> = field(request, "boundary_column_type")?;
    coverage_query(
        &replay(&request.getattr("replay")?)?,
        boundary_column_type.as_deref(),
    )
    .map_err(|error| replay_error(py, error))
}

/// One direct replay for an `AdapterCapturedReplayRequest`.
#[pyfunction]
fn render_clickhouse_replay_from_capture(
    py: Python<'_>,
    request: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let boundary_column_type: Option<String> = field(request, "boundary_column_type")?;
    let lower_bounds: Vec<LowerBound> = request
        .getattr("lower_bounds")?
        .try_iter()?
        .map(|lower_bound| {
            let lower_bound = lower_bound?;
            Ok(LowerBound {
                value: field(&lower_bound, "value")?,
                partition_value: field(&lower_bound, "partition_value")?,
            })
        })
        .collect::<PyResult<_>>()?;
    replay_from_capture(
        &replay(&request.getattr("replay")?)?,
        boundary_column_type.as_deref(),
        &lower_bounds,
    )
    .map_err(|error| replay_error(py, error))
}

/// Seed and replay statements for an `AdapterDeploymentReplayRequest`.
#[pyfunction]
fn render_clickhouse_replay_from_deployment(
    py: Python<'_>,
    request: &Bound<'_, PyAny>,
) -> PyResult<Vec<String>> {
    replay_from_deployment(&DeploymentReplay {
        replay: replay(&request.getattr("replay")?)?,
        metadata_database: field(request, "metadata_database")?,
        deployment_id: field(request, "deployment_id")?,
        boundary_column_type: field(request, "boundary_column_type")?,
        active_relation_name: field(request, "active_relation_name")?,
        active_column_names: field(request, "active_column_names")?,
        anchor_column_names: field(request, "anchor_column_names")?,
    })
    .map_err(|error| replay_error(py, error))
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
    module.add_function(wrap_pyfunction!(
        render_clickhouse_replay_coverage_query,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        render_clickhouse_replay_from_capture,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        render_clickhouse_replay_from_deployment,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(database_scoped_consumer_group, module)?)
}
