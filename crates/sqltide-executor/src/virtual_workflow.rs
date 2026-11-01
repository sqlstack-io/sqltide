//! Every statement of a virtual build, in execution order, from pre-rendered adapter SQL.
//!
//! The adapter renders resources, metadata, and replays; this module writes the assertions,
//! boundary and watermark captures, readiness checks, and the order everything runs in.

use crate::workflow::{
    Intent, Phase, Statement, escape_literal, step_segment, terminate_sql, wait_sql,
};

const DEPLOYMENTS_TABLE: &str = "_sqltide_virtual_deployments";
const WATERMARKS_TABLE: &str = "_sqltide_virtual_replay_boundaries";
const PUBLISH_HISTORY_TABLE: &str = "_sqltide_virtual_publications";
const WATERMARK_COLUMNS: &str = "(deployment_id, root_database_name, root_object_type, \
    root_object_name, anchor_database_name, anchor_object_type, anchor_object_name, \
    boundary_kind, value_kind, partition_value, lower_value, cutoff_value, cutoff_inclusive, \
    captured_at)";

/// How a replay bounds its input.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Offsets,
    Cursor,
    Timestamp,
    LandedAt,
}

impl Mode {
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "offsets" => Self::Offsets,
            "cursor" => Self::Cursor,
            "timestamp" => Self::Timestamp,
            "landed_at" => Self::LandedAt,
            _ => return None,
        })
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Offsets => "offsets",
            Self::Cursor => "cursor",
            Self::Timestamp => "timestamp",
            Self::LandedAt => "landed_at",
        }
    }

    const fn value_kind(self) -> &'static str {
        match self {
            Self::Cursor => "integer",
            _ => "timestamp",
        }
    }

    const fn type_expression(self) -> &'static str {
        match self {
            Self::Cursor => "UInt64",
            _ => "DateTime64(3, 'UTC')",
        }
    }
}

/// The parts of a replay request the workflow's own SQL reads.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Replay {
    pub mode: Mode,
    pub database: String,
    pub root: String,
    pub anchor: String,
    pub target: String,
    pub partition_column: String,
    pub offset_column: String,
    pub timestamp_column: String,
    pub landed_at_column: String,
    pub cursor_column: String,
    /// The window's lower bound is a forced start time.
    pub forced_lower_bound: bool,
    pub forced_start_time: Option<String>,
}

impl Replay {
    fn position_column(&self) -> &str {
        match self.mode {
            Mode::Offsets => &self.offset_column,
            Mode::Cursor => &self.cursor_column,
            Mode::Timestamp => &self.timestamp_column,
            Mode::LandedAt => &self.landed_at_column,
        }
    }

    /// Landed-at lineage when projected, else the timestamp.
    fn arrival_column(&self) -> &str {
        if self.landed_at_column.is_empty() {
            &self.timestamp_column
        } else {
            &self.landed_at_column
        }
    }
}

/// One replayed root.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Root {
    pub name: String,
    pub replay: Replay,
    pub persist_watermarks: bool,
    /// `(database, object type, name)` of the upstream boundary.
    pub boundary_key: (Option<String>, String, String),
    pub anchor_existed_at_confirmation: bool,
    /// The adapter's seed statements, then its replay statement.
    pub rendered: Vec<String>,
}

/// The recorded candidate deployment.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Deployment {
    pub deployment_id: String,
    pub created_at: String,
    pub replay_lineage_mode: String,
    pub workflow_fingerprint: String,
    pub tool_version: String,
}

/// Everything a virtual build's statements are assembled from.
#[derive(Clone, Debug, PartialEq)]
pub struct Input {
    pub metadata_database: String,
    pub default_database: String,
    /// The confirmed deployment ID the workflow targets.
    pub deployment_id: String,
    pub deployment: Deployment,
    pub boundary_time: Option<String>,
    pub deployments_table_exists: bool,
    pub publish_table_exists: bool,
    /// Each prepared relation, and the `CREATE` SQL it had at confirmation when it existed.
    pub prepared: Vec<(String, Option<String>)>,
    pub ensure_database_sql: String,
    pub migrate_metadata_sql: Vec<String>,
    /// `(resource name, SQL)` of absent source resources.
    pub source_realizations: Vec<(String, String)>,
    pub persist_metadata_sql: Vec<String>,
    /// `(resource name, SQL)` of staged objects.
    pub realizations: Vec<(String, String)>,
    /// `(landing view name, SQL)` of source landing views to attach.
    pub landing_views: Vec<(String, String)>,
    pub stabilization_seconds: f64,
    pub roots: Vec<Root>,
}

/// Every statement of the workflow, in order.
#[must_use]
pub fn statements(input: &Input) -> Vec<Statement> {
    let mut statements: Vec<Statement> = preflight(input);
    statements.push(Statement::mutation(
        "prepare_target_database",
        Phase::Preparation,
        &input.ensure_database_sql,
    ));
    for (index, sql) in input.migrate_metadata_sql.iter().enumerate() {
        statements.push(Statement::mutation(
            format!("prepare_metadata_{}", index + 1),
            Phase::Preparation,
            sql,
        ));
    }
    for (name, sql) in &input.source_realizations {
        statements.push(Statement::mutation(
            format!("prepare_source_{}", step_segment(name)),
            Phase::Preparation,
            sql,
        ));
    }
    for (index, sql) in input.persist_metadata_sql.iter().enumerate() {
        statements.push(Statement::mutation(
            format!("persist_candidate_metadata_{}", index + 1),
            Phase::Preparation,
            sql,
        ));
    }
    for (name, sql) in &input.realizations {
        statements.push(Statement::mutation(
            format!("realize_{}", step_segment(name)),
            Phase::Realization,
            sql,
        ));
    }
    for (name, sql) in &input.landing_views {
        statements.push(Statement::mutation(
            format!("attach_source_{}", step_segment(name)),
            Phase::Realization,
            sql,
        ));
    }
    statements.push(Statement::new(
        "wait_for_virtual_live_stabilization",
        Phase::Stabilization,
        Intent::Wait,
        wait_sql(input.stabilization_seconds),
    ));
    statements.push(Statement::new(
        "capture_boundary_time",
        Phase::Boundary,
        Intent::Mutation,
        boundary_time_capture_sql(input),
    ));
    for root in &input.roots {
        let segment: String = step_segment(&root.name);
        statements.push(Statement::new(
            format!("capture_watermark_{segment}"),
            Phase::Replay,
            Intent::Mutation,
            watermark_capture_sql(input, root),
        ));
        statements.push(Statement::new(
            format!("assert_qualifying_input_{segment}"),
            Phase::Replay,
            Intent::Assertion,
            qualifying_input_assertion_sql(input, root),
        ));
        for (position, sql) in root.rendered.iter().enumerate() {
            let prefix: &str = if position + 1 == root.rendered.len() {
                "replay"
            } else {
                "seed"
            };
            statements.push(Statement::mutation(
                format!("{prefix}_{segment}"),
                Phase::Replay,
                sql,
            ));
        }
    }
    for root in &input.roots {
        let segment: String = step_segment(&root.name);
        let replay: &Replay = &root.replay;
        statements.push(Statement::new(
            format!("read_readiness_{segment}"),
            Phase::Audit,
            Intent::Query,
            format!(
                "SELECT count() AS staged_row_count, '{}' AS staged_relation_name FROM {}.{};",
                escape_literal(&replay.target),
                replay.database,
                replay.target
            ),
        ));
        statements.push(Statement::new(
            format!("assert_readiness_{segment}"),
            Phase::Audit,
            Intent::Assertion,
            format!(
                "SELECT throwIf(count() != 1, 'Virtual candidate target is not ready') FROM \
                 system.tables WHERE database = '{}' AND name = '{}';",
                escape_literal(&replay.database),
                escape_literal(&replay.target)
            ),
        ));
    }
    statements.push(Statement::new(
        "read_boundary_time",
        Phase::Audit,
        Intent::Query,
        format!(
            "SELECT toString(any(boundary_time), 'UTC') AS boundary_time FROM {}.{DEPLOYMENTS_TABLE} \
             WHERE deployment_id = '{}';",
            input.metadata_database,
            escape_literal(&input.deployment_id)
        ),
    ));
    statements
}

fn preflight(input: &Input) -> Vec<Statement> {
    let mut rendered: Vec<(String, String)> = vec![
        (
            "assert_candidate_metadata".to_owned(),
            candidate_assertion_sql(input),
        ),
        (
            "assert_candidate_unpublished".to_owned(),
            publish_assertion_sql(input),
        ),
    ];
    for (name, observed) in &input.prepared {
        rendered.push((
            format!("assert_candidate_relation_{}", step_segment(name)),
            relation_assertion_sql(&input.default_database, name, observed.as_ref()),
        ));
    }
    rendered
        .into_iter()
        .map(|(step_id, sql)| {
            Statement::new(
                step_id,
                Phase::Preflight,
                Intent::Assertion,
                terminate_sql(&sql),
            )
        })
        .collect()
}

fn absent_assertion_sql(message: &str, database: &str, name: &str) -> String {
    format!(
        "SELECT throwIf(count() != 0, '{message}') FROM system.tables WHERE database = '{}' AND \
         name = '{}'",
        escape_literal(database),
        escape_literal(name)
    )
}

fn candidate_assertion_sql(input: &Input) -> String {
    if !input.deployments_table_exists {
        return absent_assertion_sql(
            "Candidate metadata appeared after virtual confirmation",
            &input.metadata_database,
            DEPLOYMENTS_TABLE,
        );
    }
    let deployment: &Deployment = &input.deployment;
    format!(
        "SELECT throwIf(count() > 0 AND (count() != 1 OR any(created_at) != toDateTime64('{}', 3, \
         'UTC') OR any(replay_lineage_mode) != '{}' OR any(workflow_fingerprint) != '{}' OR \
         any(tool_version) != '{}'), 'Candidate deployment conflicts with the confirmed \
         workflow') FROM {}.{DEPLOYMENTS_TABLE} WHERE deployment_id = '{}'",
        escape_literal(&deployment.created_at),
        escape_literal(&deployment.replay_lineage_mode),
        escape_literal(&deployment.workflow_fingerprint),
        escape_literal(&deployment.tool_version),
        input.metadata_database,
        escape_literal(&deployment.deployment_id)
    )
}

fn publish_assertion_sql(input: &Input) -> String {
    if !input.publish_table_exists {
        return absent_assertion_sql(
            "Publish metadata appeared after virtual confirmation",
            &input.metadata_database,
            PUBLISH_HISTORY_TABLE,
        );
    }
    format!(
        "SELECT throwIf(count() > 0, 'Candidate deployment is already published') FROM \
         {}.{PUBLISH_HISTORY_TABLE} WHERE deployment_id = '{}'",
        input.metadata_database,
        escape_literal(&input.deployment_id)
    )
}

fn relation_assertion_sql(database: &str, name: &str, observed: Option<&String>) -> String {
    let Some(definition_sql) = observed else {
        return absent_assertion_sql(
            "Candidate relation appeared after virtual confirmation",
            database,
            name,
        );
    };
    format!(
        "SELECT throwIf(count() != 1 OR any(create_table_query) != '{}', 'Candidate relation \
         changed after virtual confirmation') FROM system.tables WHERE database = '{}' AND name \
         = '{}'",
        escape_literal(definition_sql),
        escape_literal(database),
        escape_literal(name)
    )
}

fn boundary_time_capture_sql(input: &Input) -> String {
    let deployment: &Deployment = &input.deployment;
    let boundary_expression: String = input.boundary_time.as_ref().map_or_else(
        || "now64(3, 'UTC')".to_owned(),
        |time| format!("toDateTime64('{}', 3, 'UTC')", escape_literal(time)),
    );
    let table: String = format!("{}.{DEPLOYMENTS_TABLE}", input.metadata_database);
    let deployment_id: String = escape_literal(&deployment.deployment_id);
    format!(
        "INSERT INTO {table} (deployment_id, workflow_fingerprint, replay_lineage_mode, \
         boundary_time, created_at, tool_version)\nSELECT '{deployment_id}', '{}', '{}', \
         {boundary_expression}, toDateTime64('{}', 3, 'UTC'), '{}'\nWHERE NOT EXISTS (SELECT 1 \
         FROM {table} WHERE deployment_id = '{deployment_id}');",
        escape_literal(&deployment.workflow_fingerprint),
        escape_literal(&deployment.replay_lineage_mode),
        escape_literal(&deployment.created_at),
        escape_literal(&deployment.tool_version)
    )
}

fn boundary_time_subquery(input: &Input) -> String {
    format!(
        "(SELECT any(boundary_time) FROM {}.{DEPLOYMENTS_TABLE} WHERE deployment_id = '{}')",
        input.metadata_database,
        escape_literal(&input.deployment_id)
    )
}

fn nullable_literal(value: Option<&str>) -> String {
    value.map_or_else(
        || "NULL".to_owned(),
        |value| format!("'{}'", escape_literal(value)),
    )
}

fn watermark_capture_sql(input: &Input, root: &Root) -> String {
    let replay: &Replay = &root.replay;
    let table: String = format!("{}.{WATERMARKS_TABLE}", input.metadata_database);
    let deployment_id: String = escape_literal(&input.deployment_id);
    let root_name: String = escape_literal(&replay.root);
    let database: String = nullable_literal(Some(&replay.database));
    if replay.mode == Mode::Offsets {
        let predicate: String = if root.persist_watermarks {
            format!(
                "\nWHERE {} <= {}",
                replay.arrival_column(),
                boundary_time_subquery(input)
            )
        } else {
            String::new()
        };
        return format!(
            "INSERT INTO {table} {WATERMARK_COLUMNS}\nSELECT '{deployment_id}', {database}, \
             'table', '{root_name}', {database}, 'table', '{}', 'offsets', 'integer', \
             toString({partition}), NULL, toString(max({})), true, now64(3, 'UTC')\nFROM \
             {}.{}{predicate}\nGROUP BY {partition}\nHAVING NOT EXISTS (SELECT 1 FROM {table} \
             WHERE deployment_id = '{deployment_id}' AND root_object_name = '{root_name}' AND \
             boundary_kind = 'offsets');",
            escape_literal(&replay.anchor),
            replay.offset_column,
            replay.database,
            replay.anchor,
            partition = replay.partition_column,
        );
    }
    let cutoff: String = if replay.mode == Mode::Cursor {
        format!("toString(max({}))", replay.cursor_column)
    } else {
        format!("toString({}, 'UTC')", boundary_time_subquery(input))
    };
    let (anchor_database, anchor_type, anchor_name) = &root.boundary_key;
    let mode: String = escape_literal(replay.mode.as_str());
    format!(
        "INSERT INTO {table} {WATERMARK_COLUMNS}\nSELECT '{deployment_id}', {database}, 'table', \
         '{root_name}', {}, '{}', '{}', '{mode}', '{}', NULL, NULL, {cutoff}, true, now64(3, \
         'UTC')\nFROM {}.{}\nHAVING count() > 0 AND NOT EXISTS (SELECT 1 FROM {table} WHERE \
         deployment_id = '{deployment_id}' AND root_object_name = '{root_name}' AND \
         boundary_kind = '{mode}');",
        nullable_literal(anchor_database.as_deref()),
        escape_literal(anchor_type),
        escape_literal(anchor_name),
        replay.mode.value_kind(),
        replay.database,
        replay.anchor
    )
}

fn qualifying_input_assertion_sql(input: &Input, root: &Root) -> String {
    if !root.anchor_existed_at_confirmation {
        return "SELECT 0;".to_owned();
    }
    let replay: &Replay = &root.replay;
    let watermarks: String = format!(
        "(SELECT * FROM {}.{WATERMARKS_TABLE}) AS watermark",
        input.metadata_database
    );
    let deployment_id: String = escape_literal(&input.deployment_id);
    let root_name: String = escape_literal(&replay.root);
    let mode: String = escape_literal(replay.mode.as_str());
    let qualifying: String = if replay.mode == Mode::Offsets {
        format!(
            "SELECT count() FROM {}.{} AS source INNER JOIN {watermarks} ON \
             watermark.deployment_id = '{deployment_id}' AND watermark.root_object_name = \
             '{root_name}' AND watermark.boundary_kind = 'offsets' AND watermark.partition_value \
             = toString(source.{}) WHERE source.{} <= toInt64(watermark.cutoff_value)",
            replay.database, replay.anchor, replay.partition_column, replay.offset_column
        )
    } else {
        format!(
            "SELECT count() FROM {}.{} AS source CROSS JOIN {watermarks} WHERE \
             watermark.deployment_id = '{deployment_id}' AND watermark.root_object_name = \
             '{root_name}' AND watermark.boundary_kind = '{mode}' AND source.{} <= \
             CAST(watermark.cutoff_value AS {}){}",
            replay.database,
            replay.anchor,
            replay.position_column(),
            replay.mode.type_expression(),
            forced_scalar_clause(replay)
        )
    };
    format!(
        "SELECT throwIf(total_rows > 0 AND qualifying_rows = 0, 'Replay root {root_name} has \
         rows but no qualifying {mode} cutoff') FROM (SELECT (SELECT count() FROM {}.{}) AS \
         total_rows, ({qualifying}) AS qualifying_rows);",
        replay.database, replay.anchor
    )
}

fn forced_scalar_clause(replay: &Replay) -> String {
    if !replay.forced_lower_bound {
        return String::new();
    }
    let Some(start) = &replay.forced_start_time else {
        return " AND false".to_owned();
    };
    let time_column: &str = if replay.mode == Mode::LandedAt {
        &replay.landed_at_column
    } else {
        &replay.timestamp_column
    };
    format!(
        " AND source.{time_column} >= toDateTime64('{}', 3, 'UTC')",
        escape_literal(start)
    )
}

#[cfg(test)]
mod tests;
