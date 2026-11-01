//! ClickHouse SQL rendering for the Python adapter: reads the neutral adapter dataclasses and
//! returns SQL text.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString};
use sqltide_clickhouse::CapabilityError;
use sqltide_clickhouse::audit::{audit_count_query, audit_sample_query};
use sqltide_clickhouse::comparison::{SetDifferenceTarget, render_set_difference_comparison};
use sqltide_clickhouse::metadata::{
    Binding, Deployment, DirectFingerprint, Literal, Manifest, MetadataState, ObjectKey,
    ObjectState, PublishEvent, QualityNode, SensorState, Watermark,
    direct_fingerprint_observations, latest_fingerprints_query, latest_node_status_query,
    manifest_history_query, manifest_publication, metadata_state, migration_statements,
    migration_workflow, object_state_records_query, run_event_inserts, run_statement_inserts,
    run_statement_verification_query, sensor_retention_cleanup, sensor_state_inserts,
};
use sqltide_clickhouse::realization;
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

const INVOCATION_COLUMNS: [&str; 17] = [
    "invocation_id",
    "project_identity",
    "target_identity",
    "command",
    "mode",
    "outcome",
    "exit_code",
    "materialized_outcome",
    "deployment_id",
    "workflow_id",
    "selected_node_count",
    "started_at",
    "completed_at",
    "duration_ms",
    "error_message",
    "summary_json",
    "tool_version",
];
const NODE_RESULT_COLUMNS: [&str; 18] = [
    "result_id",
    "invocation_id",
    "node_kind",
    "node_name",
    "binding_key",
    "definition_fingerprint",
    "execution_fingerprint",
    "target_identity",
    "trigger",
    "scheduled_for",
    "cadence_seconds",
    "warmup_seconds",
    "status",
    "severity",
    "failure_count",
    "completed_at",
    "payload_json",
    "error_message",
];
const RUN_EVENT_COLUMNS: [&str; 6] = [
    "invocation_id",
    "sequence",
    "event_kind",
    "step_id",
    "phase",
    "payload_json",
];
const RUN_STATEMENT_COLUMNS: [&str; 9] = [
    "invocation_id",
    "statement_sequence",
    "step_id",
    "phase",
    "intent",
    "sql",
    "sql_sha256",
    "workflow_sha256",
    "workflow_revision",
];
const SENSOR_CHECKPOINT_COLUMNS: [&str; 4] = [
    "sensor_name",
    "source",
    "position_completed_at",
    "position_result_id",
];
const SENSOR_TICK_COLUMNS: [&str; 13] = [
    "tick_id",
    "sensor_name",
    "definition_fingerprint",
    "kind",
    "event_id",
    "event_kind",
    "attempt",
    "status",
    "started_at",
    "completed_at",
    "error_message",
    "skip_reason",
    "cursor",
];
const SENSOR_STEP_COLUMNS: [&str; 8] = [
    "sensor_name",
    "event_id",
    "step_key",
    "policy",
    "status",
    "attempt",
    "result_json",
    "error_message",
];
const SENSOR_OVERRIDE_COLUMNS: [&str; 4] = ["override_id", "sensor_name", "status", "actor"];
const SENSOR_LEASE_COLUMNS: [&str; 4] = ["lease_name", "owner_id", "acquired_at", "expires_at"];
const MANIFEST_RESOURCE_FIELDS: [&str; 7] = [
    "pipeline_name",
    "logical_type",
    "logical_name",
    "resource_role",
    "resource_database",
    "resource_name",
    "resource_kind",
];

fn literal(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Literal> {
    if value.is_none() {
        return Ok(Literal::Null);
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(Literal::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        return Ok(match value.extract::<i64>() {
            Ok(number) => Literal::Integer(number),
            Err(_) => Literal::Number(value.str()?.extract()?),
        });
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(Literal::Number(value.str()?.extract()?));
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(Literal::Text(text.to_str()?.to_owned()));
    }
    Err(adapter_error(
        py,
        "AdapterResultError",
        format!(
            "Cannot render ClickHouse SQL literal for {}",
            value.get_type().name()?
        ),
    ))
}

fn rows(
    py: Python<'_>,
    records: &Bound<'_, PyAny>,
    columns: &[&str],
) -> PyResult<Vec<Vec<Literal>>> {
    records
        .try_iter()?
        .map(|record| {
            let record = record?;
            columns
                .iter()
                .map(|column| literal(py, &record.getattr(*column)?))
                .collect()
        })
        .collect()
}

fn records<T>(
    records: &Bound<'_, PyAny>,
    convert: impl Fn(&Bound<'_, PyAny>) -> PyResult<T>,
) -> PyResult<Vec<T>> {
    records
        .try_iter()?
        .map(|record| convert(&record?))
        .collect()
}

fn object_key(key: &Bound<'_, PyAny>) -> PyResult<ObjectKey> {
    Ok(ObjectKey {
        database: field(key, "database")?,
        object_type: field(key, "object_type")?,
        name: field(key, "name")?,
    })
}

fn metadata_records(py: Python<'_>, state: &Bound<'_, PyAny>) -> PyResult<MetadataState> {
    Ok(MetadataState {
        object_states: records(&state.getattr("object_states")?, |record| {
            Ok(ObjectState {
                deployment_id: field(record, "deployment_id")?,
                key: object_key(&record.getattr("key")?)?,
                normalized_fingerprint: field(record, "normalized_fingerprint")?,
                normalized_query: field(record, "normalized_query")?,
                recorded_at: field(record, "recorded_at")?,
                observation_id: field(record, "observation_id")?,
                state_kind: field(record, "state_kind")?,
                physical_database_name: field(record, "physical_database_name")?,
                physical_relation_name: field(record, "physical_relation_name")?,
                logical_model_database: field(record, "logical_model_database")?,
                logical_model_name: field(record, "logical_model_name")?,
                is_selected_root: field(record, "is_selected_root")?,
            })
        })?,
        deployments: records(&state.getattr("deployments")?, |record| {
            Ok(Deployment {
                deployment_id: field(record, "deployment_id")?,
                workflow_fingerprint: field(record, "workflow_fingerprint")?,
                replay_lineage_mode: field(record, "replay_lineage_mode")?,
                boundary_time: field(record, "boundary_time")?,
                created_at: field(record, "created_at")?,
                tool_version: field(record, "tool_version")?,
            })
        })?,
        watermarks: records(&state.getattr("deployment_watermarks")?, |record| {
            Ok(Watermark {
                deployment_id: field(record, "deployment_id")?,
                root_key: object_key(&record.getattr("root_key")?)?,
                anchor_key: object_key(&record.getattr("anchor_key")?)?,
                boundary_key: field(record, "boundary_key")?,
                cutoff_value: field(record, "cutoff_value")?,
                lower_value: field(record, "lower_value")?,
                cutoff_inclusive: field(record, "cutoff_inclusive")?,
                captured_at: field(record, "captured_at")?,
            })
        })?,
        publish_events: records(&state.getattr("publish_events")?, |record| {
            Ok(PublishEvent {
                deployment_id: field(record, "deployment_id")?,
                published_at: field(record, "published_at")?,
                publication_id: field(record, "publication_id")?,
                bindings: records(&record.getattr("bindings")?, |binding| {
                    Ok(Binding {
                        database: field(binding, "database")?,
                        logical_name: field(binding, "logical_name")?,
                        physical_name: field(binding, "physical_name")?,
                    })
                })?,
                operation: field(record, "operation")?,
                previous_deployment_id: field(record, "previous_deployment_id")?,
            })
        })?,
        invocations: rows(py, &state.getattr("invocations")?, &INVOCATION_COLUMNS)?,
        node_results: rows(py, &state.getattr("node_results")?, &NODE_RESULT_COLUMNS)?,
    })
}

/// The current additive metadata migration, unterminated.
#[pyfunction]
fn render_clickhouse_metadata_migration_statements(database: &str) -> Vec<String> {
    migration_statements(database)
}

/// The complete idempotent metadata migration as executable SQL.
#[pyfunction]
fn render_clickhouse_metadata_migration_workflow(database: &str) -> Vec<String> {
    migration_workflow(database)
}

/// Exact inserts for every non-empty table of one `AdapterMetadataState`.
#[pyfunction]
fn render_clickhouse_metadata_state(
    py: Python<'_>,
    database: &str,
    state: &Bound<'_, PyAny>,
) -> PyResult<Vec<String>> {
    Ok(metadata_state(database, &metadata_records(py, state)?))
}

/// The latest direct SQL baseline per logical model identity.
#[pyfunction]
fn render_clickhouse_latest_fingerprints_query(
    database: &str,
    logical_model_identities: Vec<String>,
) -> String {
    latest_fingerprints_query(database, &logical_model_identities)
}

/// Every persisted object-state row for one planning snapshot.
#[pyfunction]
fn render_clickhouse_object_state_records_query(database: &str) -> String {
    object_state_records_query(database)
}

/// The persisted statement hashes of one invocation, in statement order.
#[pyfunction]
fn render_clickhouse_run_statement_verification_query(
    database: &str,
    invocation_id: &str,
) -> String {
    run_statement_verification_query(database, invocation_id)
}

/// The number of rows an audit query returns.
#[pyfunction]
fn render_clickhouse_audit_count_query(query: &str) -> String {
    audit_count_query(query)
}

/// Up to `limit` failing rows of an audit query.
#[pyfunction]
fn render_clickhouse_audit_sample_query(query: &str, limit: usize) -> String {
    audit_sample_query(query, limit)
}

/// Schema initialization and inserts for `AdapterDirectFingerprintRecord`s.
#[pyfunction]
fn render_clickhouse_direct_fingerprint_observations(
    database: &str,
    fingerprints: &Bound<'_, PyAny>,
) -> PyResult<Vec<String>> {
    let fingerprints = records(fingerprints, |record| {
        Ok(DirectFingerprint {
            fingerprint_id: field(record, "fingerprint_id")?,
            logical_model_identity: field(record, "logical_model_identity")?,
            definition_sql: field(record, "definition_sql")?,
            definition_hash: field(record, "definition_hash")?,
            identity_metadata: field(record, "identity_metadata")?,
            workflow_id: field(record, "workflow_id")?,
            tool_version: field(record, "tool_version")?,
            applied_at: field(record, "applied_at")?,
        })
    })?;
    Ok(direct_fingerprint_observations(database, &fingerprints))
}

/// Complete manifest history for one project target, newest first.
#[pyfunction]
fn render_clickhouse_manifest_history_query(
    database: &str,
    project_identity: &str,
    target_name: &str,
    target_database: &str,
) -> String {
    manifest_history_query(database, project_identity, target_name, target_database)
}

/// One append containing a complete immutable `AdapterManifest`.
#[pyfunction]
fn render_clickhouse_manifest_publication(
    database: &str,
    manifest: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let resources = records(&manifest.getattr("resources")?, |resource| {
        let mut values: [String; 7] = Default::default();
        for (value, name) in values.iter_mut().zip(MANIFEST_RESOURCE_FIELDS) {
            *value = field(resource, name)?;
        }
        Ok(values)
    })?;
    Ok(manifest_publication(
        database,
        &Manifest {
            manifest_id: field(manifest, "manifest_id")?,
            invocation_id: field(manifest, "invocation_id")?,
            project_identity: field(manifest, "project_identity")?,
            target_name: field(manifest, "target_name")?,
            target_database: field(manifest, "target_database")?,
            is_production: field(manifest, "is_production")?,
            project_revision: field(manifest, "project_revision")?,
            manifest_fingerprint: field(manifest, "manifest_fingerprint")?,
            manifest_version: field(manifest, "manifest_version")?,
            pipelines: field(manifest, "pipelines")?,
            resources,
            tool_version: field(manifest, "tool_version")?,
            published_at: field(manifest, "published_at")?,
        },
    ))
}

/// Incremental inserts for one `AdapterSensorState`, optionally preceded by the migration.
#[pyfunction]
fn render_clickhouse_sensor_state_inserts(
    py: Python<'_>,
    database: &str,
    state: &Bound<'_, PyAny>,
    include_migration: bool,
) -> PyResult<Vec<String>> {
    let state = SensorState {
        checkpoints: rows(
            py,
            &state.getattr("checkpoints")?,
            &SENSOR_CHECKPOINT_COLUMNS,
        )?,
        ticks: rows(py, &state.getattr("ticks")?, &SENSOR_TICK_COLUMNS)?,
        steps: rows(py, &state.getattr("steps")?, &SENSOR_STEP_COLUMNS)?,
        overrides: rows(py, &state.getattr("overrides")?, &SENSOR_OVERRIDE_COLUMNS)?,
        leases: rows(py, &state.getattr("leases")?, &SENSOR_LEASE_COLUMNS)?,
    };
    Ok(sensor_state_inserts(database, &state, include_migration))
}

/// Bounded deletes for aged sensor ticks and steps.
#[pyfunction]
fn render_clickhouse_sensor_retention_cleanup(database: &str, retention_days: i64) -> Vec<String> {
    sensor_retention_cleanup(database, retention_days)
}

/// Inserts for `AdapterRunEventRecord`s, optionally preceded by the migration.
#[pyfunction]
fn render_clickhouse_run_event_inserts(
    py: Python<'_>,
    database: &str,
    events: &Bound<'_, PyAny>,
    include_migration: bool,
) -> PyResult<Vec<String>> {
    Ok(run_event_inserts(
        database,
        rows(py, events, &RUN_EVENT_COLUMNS)?,
        include_migration,
    ))
}

/// Inserts for `AdapterRunStatementRecord`s, optionally preceded by the migration.
#[pyfunction]
fn render_clickhouse_run_statement_inserts(
    py: Python<'_>,
    database: &str,
    statements: &Bound<'_, PyAny>,
    include_migration: bool,
) -> PyResult<Vec<String>> {
    Ok(run_statement_inserts(
        database,
        rows(py, statements, &RUN_STATEMENT_COLUMNS)?,
        include_migration,
    ))
}

/// Current manifest fingerprints joined to the latest terminal results.
#[pyfunction]
fn render_clickhouse_latest_node_status_query(
    database: &str,
    project_identity: &str,
    target_identity: &str,
    nodes: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let nodes = records(nodes, |node| {
        Ok(QualityNode {
            node_kind: field(node, "node_kind")?,
            node_name: field(node, "node_name")?,
            binding_key: field(node, "binding_key")?,
            definition_fingerprint: field(node, "definition_fingerprint")?,
            execution_fingerprint: field(node, "execution_fingerprint")?,
            cadence_seconds: field(node, "cadence_seconds")?,
            warmup_seconds: field(node, "warmup_seconds")?,
        })
    })?;
    Ok(latest_node_status_query(
        database,
        project_identity,
        target_identity,
        &nodes,
    ))
}

/// `(Kafka table, raw table, materialized view, consumer group, landing query)` names one
/// managed source realizes as. Raises `ValueError(message)` for an unrealizable source.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn realize_clickhouse_managed_source(
    logical_name: &str,
    source_kind: &str,
    format: &str,
    consumer_group: Option<&str>,
    project_name: Option<&str>,
    target_name: Option<&str>,
    setting_keys: Vec<String>,
) -> PyResult<(String, String, String, String, String)> {
    let realization = realization::realize_managed_source(&realization::ManagedSourceRequest {
        logical_name,
        source_kind,
        format,
        consumer_group,
        project_name,
        target_name,
        setting_keys: setting_keys.iter().map(String::as_str).collect(),
    })
    .map_err(PyValueError::new_err)?;
    Ok((
        realization.kafka_table_name,
        realization.raw_table_name,
        realization.materialized_view_name,
        realization.consumer_group,
        realization.landing_query,
    ))
}

/// The raw landing table's `(name, type)` columns.
#[pyfunction]
fn clickhouse_raw_landing_columns() -> Vec<(&'static str, &'static str)> {
    realization::RAW_LANDING_COLUMNS.to_vec()
}

/// The relation SQL an adopted table is read through, from its authored
/// `(partition, offset, timestamp, landed at, cursor)` lineage columns.
#[pyfunction]
fn clickhouse_external_source_relation_sql(
    table_name: &str,
    columns: [Option<String>; 5],
) -> String {
    realization::external_source_relation_sql(table_name, columns.each_ref().map(Option::as_deref))
}

/// The `postgresql()` table function a refresh source reads.
#[pyfunction]
fn clickhouse_postgres_source_relation_sql(
    host: &str,
    port: &str,
    database: &str,
    table: &str,
    user: &str,
    password_env: Option<&str>,
) -> String {
    realization::postgres_source_relation_sql(host, port, database, table, user, password_env)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(realize_clickhouse_managed_source, module)?)?;
    module.add_function(wrap_pyfunction!(clickhouse_raw_landing_columns, module)?)?;
    module.add_function(wrap_pyfunction!(
        clickhouse_external_source_relation_sql,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        clickhouse_postgres_source_relation_sql,
        module
    )?)?;
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
    module.add_function(wrap_pyfunction!(database_scoped_consumer_group, module)?)?;
    for function in [
        wrap_pyfunction!(render_clickhouse_metadata_migration_statements, module)?,
        wrap_pyfunction!(render_clickhouse_metadata_migration_workflow, module)?,
        wrap_pyfunction!(render_clickhouse_metadata_state, module)?,
        wrap_pyfunction!(render_clickhouse_latest_fingerprints_query, module)?,
        wrap_pyfunction!(render_clickhouse_direct_fingerprint_observations, module)?,
        wrap_pyfunction!(render_clickhouse_manifest_history_query, module)?,
        wrap_pyfunction!(render_clickhouse_manifest_publication, module)?,
        wrap_pyfunction!(render_clickhouse_sensor_state_inserts, module)?,
        wrap_pyfunction!(render_clickhouse_sensor_retention_cleanup, module)?,
        wrap_pyfunction!(render_clickhouse_run_event_inserts, module)?,
        wrap_pyfunction!(render_clickhouse_run_statement_inserts, module)?,
        wrap_pyfunction!(render_clickhouse_latest_node_status_query, module)?,
        wrap_pyfunction!(render_clickhouse_object_state_records_query, module)?,
        wrap_pyfunction!(render_clickhouse_run_statement_verification_query, module)?,
        wrap_pyfunction!(render_clickhouse_audit_count_query, module)?,
        wrap_pyfunction!(render_clickhouse_audit_sample_query, module)?,
    ] {
        module.add_function(function)?;
    }
    Ok(())
}
