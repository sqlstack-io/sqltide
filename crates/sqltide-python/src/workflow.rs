//! Python bindings for workflow statement assembly. Statements return as
//! `(step ID, phase, intent, SQL, continue on error)`, numbered by position from one.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::direct_replay;
use sqltide_executor::direct_workflow::{
    Input as DirectInput, Recreation, RuntimeReplay, statements as direct_statements,
};
use sqltide_executor::virtual_workflow::{Deployment, Input, Mode, Replay, Root, statements};
use sqltide_executor::workflow::{Statement, step_segment, terminate_sql};

/// `(step ID, phase, intent, SQL, continue on error)`.
type StatementResult = (String, &'static str, &'static str, String, bool);

fn field<'py, T: FromPyObject<'py>>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<T> {
    object.getattr(name)?.extract()
}

fn text(object: &Bound<'_, PyAny>, name: &str) -> PyResult<String> {
    object.getattr(name)?.str()?.extract()
}

pub(crate) fn statement_results(statements: Vec<Statement>) -> Vec<StatementResult> {
    statements
        .into_iter()
        .map(|statement| {
            (
                statement.step_id,
                statement.phase.as_str(),
                statement.intent.as_str(),
                statement.sql,
                statement.continue_on_error,
            )
        })
        .collect()
}

/// The parts of an `AdapterReplayRequest` the workflow's own SQL reads.
fn replay(object: &Bound<'_, PyAny>) -> PyResult<Replay> {
    let mode_text: String = text(object, "mode")?;
    let mode: Mode = Mode::parse(&mode_text)
        .ok_or_else(|| PyValueError::new_err(format!("Unknown replay mode '{mode_text}'")))?;
    let relations = object.getattr("relations")?;
    let columns = object.getattr("columns")?;
    let window = object.getattr("window")?;
    Ok(Replay {
        mode,
        database: field(object, "database")?,
        root: field(&relations, "root")?,
        anchor: field(&relations, "anchor")?,
        target: field(&relations, "target")?,
        partition_column: field(&columns, "partition")?,
        offset_column: field(&columns, "offset")?,
        timestamp_column: field(&columns, "timestamp")?,
        landed_at_column: field(&columns, "landed_at")?,
        cursor_column: field(&columns, "cursor")?,
        forced_lower_bound: text(&window, "lower_bound_mode")? == "forced_time",
        forced_start_time: field(&window, "forced_start_time")?,
    })
}

/// `(root name, replay request, persist watermarks, (boundary database, type, name), anchor
/// existed at confirmation, rendered seed and replay SQL)`.
type RootArgument<'py> = (
    String,
    Bound<'py, PyAny>,
    bool,
    (Option<String>, String, String),
    bool,
    Vec<String>,
);

/// `(metadata database, default database, deployment ID, (deployment ID, created at, replay
/// lineage mode, workflow fingerprint, tool version), boundary time, deployments table exists,
/// publish table exists)`.
type ContextArgument = (
    String,
    String,
    String,
    (String, String, String, String, String),
    Option<String>,
    bool,
    bool,
);

/// `(prepared relations with their confirmed SQL, ensure database SQL, metadata migrations,
/// source realizations, metadata persistence, realizations, landing views, stabilization
/// seconds)`.
type RenderedArgument = (
    Vec<(String, Option<String>)>,
    String,
    Vec<String>,
    Vec<(String, String)>,
    Vec<String>,
    Vec<(String, String)>,
    Vec<(String, String)>,
    f64,
);

/// Every statement of a virtual build, in order.
#[pyfunction]
fn virtual_workflow_statements(
    context: ContextArgument,
    rendered: RenderedArgument,
    roots: Vec<RootArgument<'_>>,
) -> PyResult<Vec<StatementResult>> {
    let (
        metadata_database,
        default_database,
        deployment_id,
        deployment,
        boundary_time,
        deployments_table_exists,
        publish_table_exists,
    ) = context;
    let (
        prepared,
        ensure_database_sql,
        migrate_metadata_sql,
        source_realizations,
        persist_metadata_sql,
        realizations,
        landing_views,
        stabilization_seconds,
    ) = rendered;
    let (id, created_at, replay_lineage_mode, workflow_fingerprint, tool_version) = deployment;
    let roots: Vec<Root> = roots
        .into_iter()
        .map(
            |(name, request, persist_watermarks, boundary_key, anchor_existed, rendered)| {
                Ok(Root {
                    name,
                    replay: replay(&request)?,
                    persist_watermarks,
                    boundary_key,
                    anchor_existed_at_confirmation: anchor_existed,
                    rendered,
                })
            },
        )
        .collect::<PyResult<_>>()?;
    Ok(statement_results(statements(&Input {
        metadata_database,
        default_database,
        deployment_id,
        deployment: Deployment {
            deployment_id: id,
            created_at,
            replay_lineage_mode,
            workflow_fingerprint,
            tool_version,
        },
        boundary_time,
        deployments_table_exists,
        publish_table_exists,
        prepared,
        ensure_database_sql,
        migrate_metadata_sql,
        source_realizations,
        persist_metadata_sql,
        realizations,
        landing_views,
        stabilization_seconds,
        roots,
    })))
}

/// `(database, ensure database SQL, source realizations, recreations as (source name,
/// database, staged name, landing view name, stage SQL), preserved landing view names,
/// bootstrapped landing views, activated landing views)`.
type DirectPreparationArgument = (
    String,
    String,
    Vec<(String, String)>,
    Vec<(String, String, String, String, String)>,
    Vec<String>,
    Vec<(String, String)>,
    Vec<(String, String)>,
);

/// `(stabilization seconds, teardown as (relation name, is a view), realizations, runtime
/// replays as (model name, capture step ID, replay step ID, coverage SQL), audits as (name,
/// query), audit sample limit)`.
type DirectExecutionArgument = (
    f64,
    Vec<(String, bool)>,
    Vec<(String, String)>,
    Vec<(String, String, String, String)>,
    Vec<(String, String)>,
    usize,
);

/// `(workflow statements, source recovery statements)` of a direct build.
#[pyfunction]
fn direct_workflow_statements(
    preparation: DirectPreparationArgument,
    execution: DirectExecutionArgument,
) -> (Vec<StatementResult>, Vec<StatementResult>) {
    let (
        database,
        ensure_database_sql,
        source_realizations,
        recreations,
        preserved_landing_view_names,
        bootstrapped_landing_views,
        activations,
    ) = preparation;
    let (
        stabilization_seconds,
        teardown,
        realizations,
        runtime_replays,
        audits,
        audit_sample_limit,
    ) = execution;
    let (statements, recovery) = direct_statements(&DirectInput {
        database,
        ensure_database_sql,
        source_realizations,
        recreations: recreations
            .into_iter()
            .map(
                |(source_name, database, staged_name, landing_view_name, stage_sql)| Recreation {
                    source_name,
                    database,
                    staged_name,
                    landing_view_name,
                    stage_sql,
                },
            )
            .collect(),
        preserved_landing_view_names,
        bootstrapped_landing_views,
        activations,
        stabilization_seconds,
        teardown,
        realizations,
        runtime_replays: runtime_replays
            .into_iter()
            .map(
                |(model_name, capture_step_id, replay_step_id, coverage_sql)| RuntimeReplay {
                    model_name,
                    capture_step_id,
                    replay_step_id,
                    coverage_sql,
                },
            )
            .collect(),
        audits,
        audit_sample_limit,
    });
    (statement_results(statements), statement_results(recovery))
}

/// `(name, ((column name, type), ...))`.
type RelationArgument = (String, Vec<(String, String)>);

fn relations(arguments: Vec<RelationArgument>) -> Vec<direct_replay::Relation> {
    arguments
        .into_iter()
        .map(|(name, columns)| direct_replay::Relation { name, columns })
        .collect()
}

/// `(model name, capture step ID, replay step ID, boundary column type)`.
type ResolvedReplay = (String, String, String, Option<String>);

/// The [`ResolvedReplay`] of each replay
/// `(mode, anchor, target, timestamp column, landed-at column, cursor column)`. Raises
/// `ValueError(("build", message))` or `ValueError(("exhausted",))`.
#[pyfunction]
fn resolve_direct_replays(
    entries: Vec<(String, Vec<String>)>,
    replays: Vec<(String, String, String, String, String, String)>,
    forced_start_time: bool,
    desired_tables: Vec<RelationArgument>,
    catalog: Vec<RelationArgument>,
) -> PyResult<Vec<ResolvedReplay>> {
    let replays: Vec<direct_replay::Replay> = replays
        .into_iter()
        .map(
            |(mode, anchor, target, timestamp_column, landed_at_column, cursor_column)| {
                Ok(direct_replay::Replay {
                    mode: Mode::parse(&mode).ok_or_else(|| {
                        PyValueError::new_err(format!("Unknown replay mode '{mode}'"))
                    })?,
                    anchor,
                    target,
                    timestamp_column,
                    landed_at_column,
                    cursor_column,
                })
            },
        )
        .collect::<PyResult<_>>()?;
    let resolved = direct_replay::resolve(&direct_replay::ResolveInput {
        entries,
        replays,
        forced_start_time,
        desired_tables: relations(desired_tables),
        catalog: relations(catalog),
    })
    .map_err(|error| match error {
        direct_replay::ResolveError::Exhausted => PyValueError::new_err(("exhausted",)),
        direct_replay::ResolveError::Build(message) => PyValueError::new_err(("build", message)),
    })?;
    Ok(resolved
        .into_iter()
        .map(|replay| {
            (
                replay.model_name,
                replay.capture_step_id,
                replay.replay_step_id,
                replay.boundary_column_type,
            )
        })
        .collect())
}

/// `((boundary key, cutoff, inclusive, partition), ...)`, `((lower value, partition), ...)`,
/// and `((partition, lower offset, upper offset), ...)`.
type RealizedCapture = (
    Vec<(String, String, bool, Option<String>)>,
    Vec<(String, Option<String>)>,
    Vec<(i128, i128, i128)>,
);

/// The replay inputs realized from captured `(partition, lower, cutoff, inclusive)` ranges.
/// Raises `ValueError(("partition",))` or `ValueError(("integer", text))`.
#[pyfunction]
fn realize_direct_replay_capture(
    mode: &str,
    ranges: Vec<(Option<String>, String, String, bool)>,
) -> PyResult<RealizedCapture> {
    let ranges: Vec<direct_replay::CapturedRange> = ranges
        .into_iter()
        .map(
            |(partition, lower, cutoff, cutoff_inclusive)| direct_replay::CapturedRange {
                partition,
                lower,
                cutoff,
                cutoff_inclusive,
            },
        )
        .collect();
    let realized = direct_replay::realize(mode, &ranges).map_err(|error| match error {
        direct_replay::CaptureError::MissingPartition => PyValueError::new_err(("partition",)),
        direct_replay::CaptureError::NotInteger(text) => PyValueError::new_err(("integer", text)),
    })?;
    Ok((
        realized
            .boundaries
            .into_iter()
            .map(|boundary| {
                (
                    boundary.key,
                    boundary.cutoff,
                    boundary.cutoff_inclusive,
                    boundary.partition,
                )
            })
            .collect(),
        realized
            .lower_bounds
            .into_iter()
            .map(|bound| (bound.value, bound.partition))
            .collect(),
        realized
            .offset_ranges
            .into_iter()
            .map(|span| (span.partition, span.lower, span.upper))
            .collect(),
    ))
}

/// `value` as a step ID segment, with every non-alphanumeric character replaced by `_`.
#[pyfunction]
fn workflow_step_segment(value: &str) -> String {
    step_segment(value)
}

/// `sql` ending in exactly one semicolon.
#[pyfunction]
fn workflow_terminate_sql(sql: &str) -> String {
    terminate_sql(sql)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(workflow_step_segment, module)?)?;
    module.add_function(wrap_pyfunction!(workflow_terminate_sql, module)?)?;
    module.add_function(wrap_pyfunction!(virtual_workflow_statements, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_direct_replays, module)?)?;
    module.add_function(wrap_pyfunction!(realize_direct_replay_capture, module)?)?;
    module.add_function(wrap_pyfunction!(direct_workflow_statements, module)?)
}
