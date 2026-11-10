//! Python bindings for workflow statement assembly. Statements return as
//! `(step ID, phase, intent, SQL, continue on error)`, numbered by position from one.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::direct_workflow::{
    Input as DirectInput, Recreation, RuntimeReplay, statements as direct_statements,
};
use sqltide_executor::virtual_workflow::{Deployment, Input, Mode, Replay, Root, statements};
use sqltide_executor::workflow::Statement;

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

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(virtual_workflow_statements, module)?)?;
    module.add_function(wrap_pyfunction!(direct_workflow_statements, module)?)
}
