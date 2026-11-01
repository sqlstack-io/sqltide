//! Replay statements: checkpoint coverage queries and bounded `INSERT ... SELECT` replays from
//! captured boundaries or from deployment metadata.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use sqltide_sqlanalysis::{
    NamedQuery, RelationRewrite, SqlDialect, rewrite_query, select_aggregate_facts,
    validate_insert_query,
};
use sqltide_sqltext::sql_scan::{
    RelationRewrite as TemplateRewrite, append_template_predicate, merge_template_ctes,
    rewrite_template_relations,
};

const DATABASE_PLACEHOLDER: &str = "__sqltide_target_database__";
const DEPLOYMENTS_TABLE: &str = "_sqltide_virtual_deployments";
const DEPLOYMENT_WATERMARKS_TABLE: &str = "_sqltide_virtual_replay_boundaries";
const REPLAY_PARTITION: &str = "_replay_partition";
const REPLAY_OFFSET: &str = "_replay_offset";
const DIALECT: &str = "clickhouse";
const REPLAY_CUTOFF_EXPRESSION: &str = "(SELECT cutoff_value FROM replay_cutoff)";
const ACTIVE_START_OFFSETS: &str = "active_start_offsets";

static NUMERIC_SETTING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$").expect("valid numeric setting pattern")
});

/// A replay request the adapter cannot render.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayError(pub String);

type ReplayResult<T> = Result<T, ReplayError>;

fn failure<T>(message: &str) -> ReplayResult<T> {
    Err(ReplayError(message.to_owned()))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BoundaryMode {
    Offsets,
    Timestamp,
    LandedAt,
    Cursor,
}

impl BoundaryMode {
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "offsets" => Self::Offsets,
            "timestamp" => Self::Timestamp,
            "landed_at" => Self::LandedAt,
            "cursor" => Self::Cursor,
            _ => return None,
        })
    }

    fn name(self) -> &'static str {
        match self {
            Self::Offsets => "offsets",
            Self::Timestamp => "timestamp",
            Self::LandedAt => "landed_at",
            Self::Cursor => "cursor",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LowerBoundMode {
    None,
    ForcedTime,
    Lookback,
    ActiveFrontier,
}

impl LowerBoundMode {
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "none" => Self::None,
            "forced_time" => Self::ForcedTime,
            "lookback" => Self::Lookback,
            "active_frontier" => Self::ActiveFrontier,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Relations {
    pub root: String,
    pub source: String,
    pub anchor: String,
    pub target: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Columns {
    pub partition: String,
    pub offset: String,
    pub timestamp: String,
    pub landed_at: String,
    pub cursor: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Window {
    pub lower_bound_mode: LowerBoundMode,
    pub lower_bound_inclusive: bool,
    pub forced_start_time: Option<String>,
    pub lookback_seconds: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Boundary {
    pub boundary_key: String,
    pub cutoff_value: String,
    pub cutoff_inclusive: bool,
    pub partition_value: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replay {
    pub mode: BoundaryMode,
    pub database: String,
    pub relations: Relations,
    pub query: String,
    pub aggregate_semantics: bool,
    pub boundaries: Vec<Boundary>,
    pub columns: Columns,
    pub window: Window,
    pub seeds_history_prefix: bool,
    pub target_column_names: Vec<String>,
    pub settings: Vec<(String, String)>,
    pub filter_boundaries_at_source: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LowerBound {
    pub value: String,
    pub partition_value: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeploymentReplay {
    pub replay: Replay,
    pub metadata_database: String,
    pub deployment_id: String,
    pub boundary_column_type: Option<String>,
    pub active_relation_name: String,
    pub active_column_names: Vec<String>,
    pub anchor_column_names: Vec<String>,
}

/// Checkpoint coverage for the rows selected by one replay window.
///
/// # Errors
///
/// Returns a replay error when a scalar window lacks its boundary type or lower edge.
pub fn coverage_query(replay: &Replay, boundary_column_type: Option<&str>) -> ReplayResult<String> {
    if replay.mode == BoundaryMode::Offsets {
        return offset_coverage_query(replay);
    }
    scalar_coverage_query(replay, boundary_column_type)
}

/// One direct replay from process-owned typed boundaries.
///
/// # Errors
///
/// Returns a replay error for incomplete boundaries or SQL that cannot be rewritten.
pub fn replay_from_capture(
    replay: &Replay,
    boundary_column_type: Option<&str>,
    lower_bounds: &[LowerBound],
) -> ReplayResult<String> {
    if replay.mode == BoundaryMode::Offsets {
        let mut lower_by_partition: HashMap<String, String> = HashMap::new();
        for lower_bound in lower_bounds {
            let Some(partition_value) = &lower_bound.partition_value else {
                return failure("Offset replay lower bound requires a partition value");
            };
            let Some(partition) = normalized_integer(partition_value) else {
                return Err(ReplayError(format!(
                    "Offset replay lower bound partition '{partition_value}' is not an integer"
                )));
            };
            lower_by_partition.insert(partition, lower_bound.value.clone());
        }
        return offset_replay(replay, &lower_by_partition);
    }
    let default_boundary;
    let boundary: &Boundary = if let Some(first) = replay.boundaries.first() {
        first
    } else {
        default_boundary = Boundary {
            boundary_key: canonical_boundary_column(replay.mode)?.to_owned(),
            cutoff_value: String::new(),
            cutoff_inclusive: true,
            partition_value: None,
        };
        &default_boundary
    };
    let Some(column_type) = boundary_column_type else {
        return failure("Scalar captured replay requires a resolved boundary column type");
    };
    scalar_replay(
        replay,
        boundary,
        column_type,
        lower_bounds.first().map(|lower| lower.value.as_str()),
    )
}

/// Seed and replay statements that read dynamic deployment metadata.
///
/// # Errors
///
/// Returns a replay error for incomplete windows or SQL that cannot be rewritten.
pub fn replay_from_deployment(request: &DeploymentReplay) -> ReplayResult<Vec<String>> {
    let seed = deployment_seed(request)?;
    let replay = if request.replay.mode == BoundaryMode::Offsets {
        deployment_offset_replay(request)?
    } else {
        deployment_scalar_replay(request)?
    };
    Ok(seed.into_iter().chain([replay]).collect())
}

/// Python's `str(int(value))` for an ASCII integer literal, or `None` when it is not one.
fn normalized_integer(value: &str) -> Option<String> {
    let trimmed = value.trim();
    let (negative, unsigned) = match trimmed.as_bytes().first() {
        Some(b'-') => (true, &trimmed[1..]),
        Some(b'+') => (false, &trimmed[1..]),
        _ => (false, trimmed),
    };
    let valid = !unsigned.is_empty()
        && !unsigned.starts_with('_')
        && !unsigned.ends_with('_')
        && !unsigned.contains("__")
        && unsigned
            .chars()
            .all(|character| character.is_ascii_digit() || character == '_');
    if !valid {
        return None;
    }
    let digits: String = unsigned.chars().filter(char::is_ascii_digit).collect();
    let significant = digits.trim_start_matches('0');
    Some(match (significant.is_empty(), negative) {
        (true, _) => "0".to_owned(),
        (false, true) => format!("-{significant}"),
        (false, false) => significant.to_owned(),
    })
}

fn deployment_offset_replay(request: &DeploymentReplay) -> ReplayResult<String> {
    let replay = &request.replay;
    let lower_cte = deployment_offset_lower_cte(request)?;
    let source_sql = deployment_offset_source_sql(request, lower_cte.is_some());
    let mut ctes: Vec<(String, String)> = vec![(
        "cutoff_offsets".to_owned(),
        deployment_offset_cutoff_cte(request),
    )];
    if let Some(lower_cte) = lower_cte {
        ctes.push((ACTIVE_START_OFFSETS.to_owned(), lower_cte));
    }
    ctes.push(("filtered_replay_source".to_owned(), source_sql));
    let query = rewrite_replay_query(
        &replay.query,
        &[(
            replay.relations.source.clone(),
            "filtered_replay_source".to_owned(),
        )],
        None,
        &ctes,
    )?;
    build_replay_insert(replay, &query)
}

fn deployment_scalar_replay(request: &DeploymentReplay) -> ReplayResult<String> {
    let replay = &request.replay;
    let column_type = required_deployment_boundary_type(request)?;
    let ctes = vec![(
        "replay_cutoff".to_owned(),
        deployment_scalar_cutoff_cte(request, column_type),
    )];
    let lower_expression = deployment_scalar_lower_expression(request, column_type)?;
    if replay.aggregate_semantics || replay.filter_boundaries_at_source {
        let physical_predicate = dynamic_scalar_predicate(
            &format!("anchor.{}", physical_boundary_column(replay)?),
            REPLAY_CUTOFF_EXPRESSION,
            lower_expression.as_deref(),
            replay.window.lower_bound_inclusive,
        );
        let source_sql = format!(
            "SELECT anchor.* FROM {}.{} AS anchor WHERE {physical_predicate}",
            replay.database, replay.relations.anchor
        );
        let query = rewrite_replay_query(
            &replay.query,
            &[(replay.relations.source.clone(), format!("({source_sql})"))],
            None,
            &ctes,
        )?;
        return build_replay_insert(replay, &query);
    }
    let predicate = dynamic_scalar_predicate(
        canonical_boundary_column(replay.mode)?,
        REPLAY_CUTOFF_EXPRESSION,
        lower_expression.as_deref(),
        replay.window.lower_bound_inclusive,
    );
    let query = rewrite_replay_query(
        &replay.query,
        &[(
            replay.relations.source.clone(),
            format!("{}.{}", replay.database, replay.relations.anchor),
        )],
        Some(&predicate),
        &ctes,
    )?;
    build_replay_insert(replay, &query)
}

fn lower_operator(inclusive: bool) -> &'static str {
    if inclusive { "<" } else { "<=" }
}

fn deployment_seed(request: &DeploymentReplay) -> ReplayResult<Option<String>> {
    let replay = &request.replay;
    if !replay.seeds_history_prefix {
        return Ok(None);
    }
    let copyable: Vec<&String> = replay
        .target_column_names
        .iter()
        .filter(|name| request.active_column_names.contains(name))
        .collect();
    if copyable.is_empty() {
        return Ok(None);
    }
    let column_list = join(copyable.iter().map(|name| name.as_str()), ", ");
    let target = format!("{}.{}", replay.database, replay.relations.target);
    let active = format!("{}.{}", replay.database, request.active_relation_name);
    let operator = lower_operator(replay.window.lower_bound_inclusive);
    if replay.mode == BoundaryMode::Offsets {
        let Some(lower_cte) = deployment_offset_lower_cte(request)? else {
            return Ok(None);
        };
        let selected = join(copyable.iter().map(|name| format!("active.{name}")), ", ");
        return Ok(Some(format!(
            "INSERT INTO {target} ({column_list})\nWITH {ACTIVE_START_OFFSETS} AS \
             (\n{lower_cte}\n)\nSELECT {selected}\nFROM {active} AS active\nINNER JOIN \
             {ACTIVE_START_OFFSETS}\nON active.{REPLAY_PARTITION} = \
             {ACTIVE_START_OFFSETS}.{REPLAY_PARTITION}\nWHERE active.{REPLAY_OFFSET} {operator} \
             {ACTIVE_START_OFFSETS}.start_offset"
        )));
    }
    let column_type = required_deployment_boundary_type(request)?;
    let Some(lower_expression) = deployment_scalar_lower_expression(request, column_type)? else {
        return Ok(None);
    };
    Ok(Some(format!(
        "INSERT INTO {target} ({column_list})\nSELECT {column_list}\nFROM {active}\nWHERE {} \
         {operator} {lower_expression}",
        canonical_boundary_column(replay.mode)?
    )))
}

fn deployment_offset_source_sql(request: &DeploymentReplay, has_lower_bound: bool) -> String {
    let replay = &request.replay;
    let lower_join =
        offset_lower_bound_join("anchor", &replay.columns.partition, has_lower_bound, None);
    let lower_clause = offset_lower_bound_clause(
        "anchor",
        &replay.columns.offset,
        has_lower_bound,
        replay.window.lower_bound_inclusive,
        None,
    );
    let upper_clause = offset_upper_bound_clause("anchor", &replay.columns.offset);
    let selected = join(
        request
            .anchor_column_names
            .iter()
            .map(|name| format!("anchor.{name} AS {name}")),
        ", ",
    );
    format!(
        "SELECT {selected}\nFROM {}.{} AS anchor\nINNER JOIN cutoff_offsets\nON anchor.{} = \
         cutoff_offsets.{REPLAY_PARTITION}\n{lower_join}WHERE {upper_clause}\n{lower_clause}",
        replay.database, replay.relations.anchor, replay.columns.partition
    )
    .trim_end()
    .to_owned()
}

fn deployment_offset_cutoff_cte(request: &DeploymentReplay) -> String {
    format!(
        "SELECT toInt64(partition_value) AS {REPLAY_PARTITION}, max(toInt64(cutoff_value)) AS \
         cutoff_offset, true AS cutoff_inclusive\nFROM {}.{DEPLOYMENT_WATERMARKS_TABLE}\nWHERE \
         deployment_id = '{}' AND root_object_name = '{}' AND boundary_kind = 'offsets'\nGROUP \
         BY {REPLAY_PARTITION}",
        request.metadata_database,
        escape_literal(&request.deployment_id),
        escape_literal(&request.replay.relations.root)
    )
}

fn deployment_lookback(request: &DeploymentReplay) -> ReplayResult<Option<String>> {
    if request.replay.window.lower_bound_mode == LowerBoundMode::Lookback {
        deployment_lookback_time_expression(request).map(Some)
    } else {
        Ok(None)
    }
}

fn deployment_offset_lower_cte(request: &DeploymentReplay) -> ReplayResult<Option<String>> {
    let lookback = deployment_lookback(request)?;
    replay_offset_lower_cte(
        &request.replay,
        Some(&request.active_relation_name),
        lookback.as_deref(),
    )
}

fn replay_offset_lower_cte(
    replay: &Replay,
    active_relation_name: Option<&str>,
    lookback_time_expression: Option<&str>,
) -> ReplayResult<Option<String>> {
    match replay.window.lower_bound_mode {
        LowerBoundMode::None => Ok(None),
        LowerBoundMode::ActiveFrontier => {
            let active_name = required_active_relation_name(active_relation_name)?;
            Ok(Some(format!(
                "SELECT {REPLAY_PARTITION}, max({REPLAY_OFFSET}) AS start_offset FROM {}.{active_name} \
                 GROUP BY {REPLAY_PARTITION}",
                replay.database
            )))
        }
        LowerBoundMode::ForcedTime | LowerBoundMode::Lookback => {
            let lower_time = replay_lower_time_expression(replay, lookback_time_expression)?;
            let time_column = if replay.columns.landed_at.is_empty() {
                &replay.columns.timestamp
            } else {
                &replay.columns.landed_at
            };
            Ok(Some(format!(
                "SELECT {} AS {REPLAY_PARTITION}, max({}) AS start_offset FROM {}.{} WHERE \
                 {time_column} <= {lower_time} GROUP BY {}",
                replay.columns.partition,
                replay.columns.offset,
                replay.database,
                replay.relations.anchor,
                replay.columns.partition
            )))
        }
    }
}

fn deployment_scalar_cutoff_cte(request: &DeploymentReplay, column_type: &str) -> String {
    let replay = &request.replay;
    format!(
        "SELECT max(CAST(cutoff_value AS {column_type})) AS cutoff_value FROM \
         {}.{DEPLOYMENT_WATERMARKS_TABLE} WHERE deployment_id = '{}' AND root_object_name = '{}' \
         AND boundary_kind = '{}'",
        request.metadata_database,
        escape_literal(&request.deployment_id),
        escape_literal(&replay.relations.root),
        escape_literal(replay.mode.name())
    )
}

fn deployment_scalar_lower_expression(
    request: &DeploymentReplay,
    column_type: &str,
) -> ReplayResult<Option<String>> {
    let lookback = deployment_lookback(request)?;
    replay_scalar_lower_expression(
        &request.replay,
        column_type,
        Some(&request.active_relation_name),
        lookback.as_deref(),
        REPLAY_CUTOFF_EXPRESSION,
    )
}

fn replay_scalar_lower_expression(
    replay: &Replay,
    column_type: &str,
    active_relation_name: Option<&str>,
    lookback_time_expression: Option<&str>,
    upper_expression: &str,
) -> ReplayResult<Option<String>> {
    match replay.window.lower_bound_mode {
        LowerBoundMode::None => Ok(None),
        LowerBoundMode::ActiveFrontier => {
            let active_name = required_active_relation_name(active_relation_name)?;
            Ok(Some(format!(
                "(SELECT max({}) FROM {}.{active_name})",
                canonical_boundary_column(replay.mode)?,
                replay.database
            )))
        }
        LowerBoundMode::ForcedTime | LowerBoundMode::Lookback => {
            let lower_time = replay_lower_time_expression(replay, lookback_time_expression)?;
            if replay.mode != BoundaryMode::Cursor {
                return Ok(Some(format!("CAST({lower_time} AS {column_type})")));
            }
            Ok(Some(format!(
                "(SELECT minOrNull({cursor}) FROM {}.{} WHERE {} >= {lower_time} AND {cursor} <= \
                 {upper_expression})",
                replay.database,
                replay.relations.anchor,
                replay.columns.timestamp,
                cursor = replay.columns.cursor
            )))
        }
    }
}

fn replay_lower_time_expression(
    replay: &Replay,
    lookback_time_expression: Option<&str>,
) -> ReplayResult<String> {
    if replay.window.lower_bound_mode == LowerBoundMode::ForcedTime {
        let Some(start_time) = &replay.window.forced_start_time else {
            return failure("Forced replay requires a start time");
        };
        return Ok(format!(
            "toDateTime64('{}', 3, 'UTC')",
            escape_literal(start_time)
        ));
    }
    match lookback_time_expression {
        Some(expression) => Ok(expression.to_owned()),
        None => failure("Lookback replay requires a lower-time expression"),
    }
}

fn deployment_lookback_time_expression(request: &DeploymentReplay) -> ReplayResult<String> {
    let Some(lookback_seconds) = request.replay.window.lookback_seconds else {
        return failure("Lookback deployment replay requires a duration");
    };
    Ok(format!(
        "(SELECT any(boundary_time) - toIntervalSecond({lookback_seconds}) FROM \
         {}.{DEPLOYMENTS_TABLE} WHERE deployment_id = '{}')",
        request.metadata_database,
        escape_literal(&request.deployment_id)
    ))
}

fn required_active_relation_name(active_relation_name: Option<&str>) -> ReplayResult<&str> {
    match active_relation_name {
        Some(name) => Ok(name),
        None => failure("Active-frontier replay requires an active relation"),
    }
}

fn dynamic_scalar_predicate(
    column_name: &str,
    upper_expression: &str,
    lower_expression: Option<&str>,
    inclusive: bool,
) -> String {
    let upper = format!("{column_name} <= {upper_expression}");
    match lower_expression {
        None => upper,
        Some(lower) => format!(
            "{column_name} {} {lower} AND {upper}",
            if inclusive { ">=" } else { ">" }
        ),
    }
}

fn required_deployment_boundary_type(request: &DeploymentReplay) -> ReplayResult<&str> {
    match &request.boundary_column_type {
        Some(column_type) => Ok(column_type),
        None => failure("Scalar deployment replay requires a resolved boundary type"),
    }
}

fn coverage_columns(replay: &Replay, timestamp_column: &str) -> String {
    format!(
        "SELECT '{}' AS driving_input_relation_name, 'offsets' AS replay_boundary_mode, \
         concat('_replay_partition=', toString(partition_value)) AS boundary_key, \
         toString(partition_value) AS partition_value, '{}' AS source_partition_column_name, '{}' \
         AS source_position_column_name, '{}' AS source_timestamp_column_name, \
         toString(lower_value) AS lower_value, toString(upper_value) AS upper_value, \
         toString(upper_value) AS replay_cutoff_value, true AS cutoff_inclusive, now64(9, 'UTC') \
         AS captured_at\nFROM (\nSELECT partition_value, min(offset_value) AS lower_value, \
         max(offset_value) AS upper_value\nFROM (\nSELECT {partition} AS partition_value, {offset} \
         AS offset_value, {offset} - toInt64(row_number() OVER (PARTITION BY {partition} ORDER BY \
         {offset})) AS sequence_group\n",
        escape_literal(&replay.relations.anchor),
        escape_literal(&replay.columns.partition),
        escape_literal(&replay.columns.offset),
        escape_literal(timestamp_column),
        partition = replay.columns.partition,
        offset = replay.columns.offset,
    )
}

const COVERAGE_TAIL: &str =
    "GROUP BY partition_value, sequence_group\nORDER BY partition_value, lower_value\n)";

fn offset_coverage_query(replay: &Replay) -> ReplayResult<String> {
    let timestamp_column = replay.columns.timestamp.as_str();
    let partition = &replay.columns.partition;
    let offset = &replay.columns.offset;
    let anchor = format!("{}.{}", replay.database, replay.relations.anchor);
    let Some(lower_cte) = replay_offset_lower_cte(replay, None, None)? else {
        return Ok(format!(
            "{}FROM (SELECT DISTINCT {partition}, {offset} FROM {anchor})\n)\n{COVERAGE_TAIL}",
            coverage_columns(replay, timestamp_column)
        ));
    };
    let lower_alias = Some("coverage_start_offsets");
    let lower_join = offset_lower_bound_join("anchor", partition, true, lower_alias);
    let lower_clause = offset_lower_bound_clause(
        "anchor",
        offset,
        true,
        replay.window.lower_bound_inclusive,
        lower_alias,
    );
    Ok(format!(
        "WITH {ACTIVE_START_OFFSETS} AS (\n{lower_cte}\n)\n{}FROM (SELECT DISTINCT \
         anchor.{partition} AS {partition}, anchor.{offset} AS {offset} FROM {anchor} AS \
         anchor\n{lower_join}WHERE true\n{lower_clause})\n)\n{COVERAGE_TAIL}",
        coverage_columns(replay, timestamp_column)
    ))
}

fn scalar_coverage_columns(replay: &Replay, position_column: &str, canonical_key: &str) -> String {
    format!(
        "SELECT '{}' AS driving_input_relation_name, '{}' AS replay_boundary_mode, '{canonical_key}' \
         AS boundary_key, CAST(NULL AS Nullable(String)) AS partition_value, '' AS \
         source_partition_column_name, '{}' AS source_position_column_name, '{}' AS \
         source_timestamp_column_name, toString(lower_value) AS lower_value, \
         toString(upper_value) AS upper_value, toString(cutoff_value) AS replay_cutoff_value, \
         true AS cutoff_inclusive, now64(9, 'UTC') AS captured_at\n",
        escape_literal(&replay.relations.anchor),
        replay.mode.name(),
        escape_literal(position_column),
        escape_literal(&replay.columns.timestamp),
    )
}

fn scalar_coverage_query(
    replay: &Replay,
    boundary_column_type: Option<&str>,
) -> ReplayResult<String> {
    let position_column = physical_boundary_column(replay)?;
    let canonical_key = canonical_boundary_column(replay.mode)?;
    let anchor = format!("{}.{}", replay.database, replay.relations.anchor);
    let columns = scalar_coverage_columns(replay, position_column, canonical_key);
    if replay.window.lower_bound_mode == LowerBoundMode::None {
        let cutoff_expression = if replay.mode == BoundaryMode::Cursor {
            format!("max({position_column})")
        } else {
            "now64(3, 'UTC')".to_owned()
        };
        return Ok(format!(
            "{columns}FROM (SELECT min({position_column}) AS lower_value, max({position_column}) \
             AS upper_value, {cutoff_expression} AS cutoff_value FROM {anchor} HAVING count() > 0)"
        ));
    }
    let Some(column_type) = boundary_column_type else {
        return failure("Scalar replay coverage requires a resolved boundary type");
    };
    let lower_expression =
        replay_scalar_lower_expression(replay, column_type, None, None, REPLAY_CUTOFF_EXPRESSION)?;
    let coverage_predicate = dynamic_scalar_predicate(
        position_column,
        REPLAY_CUTOFF_EXPRESSION,
        lower_expression.as_deref(),
        replay.window.lower_bound_inclusive,
    );
    let cutoff_query = if replay.mode == BoundaryMode::Cursor {
        format!("SELECT max({position_column}) AS cutoff_value FROM {anchor}")
    } else {
        "SELECT now64(3, 'UTC') AS cutoff_value".to_owned()
    };
    Ok(format!(
        "WITH replay_cutoff AS ({cutoff_query})\n{columns}FROM (SELECT min({position_column}) AS \
         lower_value, max({position_column}) AS upper_value, {REPLAY_CUTOFF_EXPRESSION} AS \
         cutoff_value FROM {anchor} WHERE {coverage_predicate} HAVING count() > 0)"
    ))
}

fn canonical_boundary_column(mode: BoundaryMode) -> ReplayResult<&'static str> {
    match mode {
        BoundaryMode::Cursor => Ok("_replay_cursor"),
        BoundaryMode::Timestamp => Ok("_replay_timestamp"),
        BoundaryMode::LandedAt => Ok("_replay_landed_at"),
        BoundaryMode::Offsets => failure("Offset replay has no scalar boundary column"),
    }
}

fn physical_boundary_column(replay: &Replay) -> ReplayResult<&str> {
    match replay.mode {
        BoundaryMode::Cursor => Ok(&replay.columns.cursor),
        BoundaryMode::Timestamp => Ok(&replay.columns.timestamp),
        BoundaryMode::LandedAt => Ok(&replay.columns.landed_at),
        BoundaryMode::Offsets => failure("Offset replay has no scalar boundary column"),
    }
}

fn escape_literal(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\'', "\\'")
}

fn cast_literal(value: &str, column_type: &str) -> String {
    format!("CAST('{}' AS {column_type})", value.replace('\'', "''"))
}

fn scalar_replay(
    replay: &Replay,
    boundary: &Boundary,
    column_type: &str,
    lower_bound_value: Option<&str>,
) -> ReplayResult<String> {
    let anchor = format!("{}.{}", replay.database, replay.relations.anchor);
    let source_rewrite = [(replay.relations.source.clone(), anchor.clone())];
    if boundary.cutoff_value.is_empty() {
        let query = rewrite_replay_query(&replay.query, &source_rewrite, Some("0"), &[])?;
        return build_replay_insert(replay, &query);
    }
    let lower_inclusive = replay.window.lower_bound_inclusive;
    let query = if replay.aggregate_semantics || replay.filter_boundaries_at_source {
        let physical_predicate = scalar_boundary_predicate(
            &format!("anchor.{}", physical_boundary_column(replay)?),
            boundary,
            column_type,
            lower_bound_value,
            lower_inclusive,
        );
        let filtered_anchor =
            format!("SELECT anchor.* FROM {anchor} AS anchor WHERE {physical_predicate}");
        rewrite_replay_query(
            &replay.query,
            &[(
                replay.relations.source.clone(),
                format!("({filtered_anchor})"),
            )],
            None,
            &[],
        )?
    } else {
        let canonical_predicate = scalar_boundary_predicate(
            &boundary.boundary_key,
            boundary,
            column_type,
            lower_bound_value,
            lower_inclusive,
        );
        rewrite_replay_query(
            &replay.query,
            &source_rewrite,
            Some(&canonical_predicate),
            &[],
        )?
    };
    build_replay_insert(replay, &query)
}

fn scalar_boundary_predicate(
    column_name: &str,
    boundary: &Boundary,
    column_type: &str,
    lower_bound_value: Option<&str>,
    lower_bound_inclusive: bool,
) -> String {
    let upper = format!(
        "{column_name} {} {}",
        if boundary.cutoff_inclusive { "<=" } else { "<" },
        cast_literal(&boundary.cutoff_value, column_type)
    );
    match lower_bound_value {
        None => upper,
        Some(lower) => format!(
            "{column_name} {} {} AND {upper}",
            if lower_bound_inclusive { ">=" } else { ">" },
            cast_literal(lower, column_type)
        ),
    }
}

fn offset_replay(
    replay: &Replay,
    lower_by_partition: &HashMap<String, String>,
) -> ReplayResult<String> {
    let predicate = offset_literal_predicate(replay, lower_by_partition)?;
    let filtered_source = format!(
        "SELECT anchor.*\nFROM {}.{} AS anchor\nWHERE {predicate}",
        replay.database, replay.relations.anchor
    );
    let query = rewrite_replay_query(
        &replay.query,
        &[(
            replay.relations.source.clone(),
            format!("({filtered_source})"),
        )],
        None,
        &[],
    )?;
    build_replay_insert(replay, &query)
}

fn offset_literal_predicate(
    replay: &Replay,
    lower_by_partition: &HashMap<String, String>,
) -> ReplayResult<String> {
    let mut predicates: Vec<String> = Vec::new();
    for boundary in &replay.boundaries {
        let Some(partition_value) = &boundary.partition_value else {
            return failure("Offset replay boundary requires a partition value");
        };
        let mut clauses: Vec<String> = vec![
            format!("anchor.{} = {partition_value}", replay.columns.partition),
            format!(
                "anchor.{} {} {}",
                replay.columns.offset,
                if boundary.cutoff_inclusive { "<=" } else { "<" },
                boundary.cutoff_value
            ),
        ];
        if let Some(lower_value) = lower_by_partition.get(partition_value) {
            clauses.push(format!(
                "anchor.{} {} {lower_value}",
                replay.columns.offset,
                if replay.window.lower_bound_inclusive {
                    ">="
                } else {
                    ">"
                }
            ));
        }
        predicates.push(format!("({})", clauses.join(" AND ")));
    }
    Ok(match predicates.as_slice() {
        [] => "false".to_owned(),
        [single] => single.clone(),
        _ => format!("(\n  {}\n)", predicates.join("\n  OR ")),
    })
}

fn rewrite_replay_query(
    sql: &str,
    relation_rewrites: &[(String, String)],
    predicate: Option<&str>,
    ctes: &[(String, String)],
) -> ReplayResult<String> {
    let dialect = SqlDialect::new(DIALECT);
    let rewrite_failure =
        |message: String| ReplayError(format!("Replay SQL could not be rewritten: {message}"));
    if sql.contains(DATABASE_PLACEHOLDER) {
        let rewrites: Vec<TemplateRewrite> = relation_rewrites
            .iter()
            .map(|(source_name, target_relation)| TemplateRewrite {
                source_name: source_name.clone(),
                target_relation: target_relation.clone(),
                preserve_source_database: false,
            })
            .collect();
        let mut rewritten = rewrite_template_relations(sql, &rewrites, DATABASE_PLACEHOLDER);
        if let Some(predicate) = predicate {
            rewritten = append_template_predicate(&chars(&rewritten), predicate)
                .map_err(|error| rewrite_failure(error.message))?;
        }
        rewritten = merge_template_ctes(&chars(&rewritten), ctes)
            .map_err(|error| rewrite_failure(error.message))?;
        select_aggregate_facts(&rewritten, &dialect).map_err(|error| rewrite_failure(error.0))?;
        return Ok(rewritten);
    }
    let rewrites: Vec<RelationRewrite> = relation_rewrites
        .iter()
        .map(|(source_name, target_relation)| RelationRewrite {
            source_name: source_name.clone(),
            target_relation: target_relation.clone(),
            source_databases: None,
            preserve_source_database: false,
        })
        .collect();
    let named: Vec<NamedQuery> = ctes
        .iter()
        .map(|(name, query)| NamedQuery {
            name: name.clone(),
            query: query.clone(),
        })
        .collect();
    rewrite_query(sql, &dialect, &rewrites, predicate, &named)
        .map(|rewritten| rewritten.query)
        .map_err(|error| rewrite_failure(error.0))
}

fn chars(text: &str) -> Vec<char> {
    text.chars().collect()
}

fn build_replay_insert(replay: &Replay, query: &str) -> ReplayResult<String> {
    let mut statement = format!(
        "INSERT INTO {}.{}\n{query}",
        replay.database, replay.relations.target
    );
    validate_insert_query(&statement, &SqlDialect::new(DIALECT))
        .map_err(|error| ReplayError(format!("Replay SQL could not be generated: {}", error.0)))?;
    if statement.contains(DATABASE_PLACEHOLDER) {
        return failure("Replay SQL leaked the adapter database placeholder");
    }
    if !replay.settings.is_empty() {
        let settings = join(
            replay
                .settings
                .iter()
                .map(|(name, value)| format!("{name} = {}", setting_value(value))),
            ", ",
        );
        statement.push_str(&format!("\nSETTINGS {settings}"));
    }
    Ok(statement)
}

fn setting_value(value: &str) -> String {
    let lowered = value.to_lowercase();
    if NUMERIC_SETTING.is_match(value) || lowered == "true" || lowered == "false" {
        return lowered;
    }
    format!("'{}'", value.replace('\'', "''"))
}

fn offset_lower_bound_join(
    source_alias: &str,
    partition_column: &str,
    has_lower_bound: bool,
    lower_alias: Option<&str>,
) -> String {
    if !has_lower_bound {
        return String::new();
    }
    let relation = lower_alias.map_or_else(
        || ACTIVE_START_OFFSETS.to_owned(),
        |alias| format!("{ACTIVE_START_OFFSETS} AS {alias}"),
    );
    let reference = lower_alias.unwrap_or(ACTIVE_START_OFFSETS);
    format!(
        "LEFT JOIN {relation}\nON {source_alias}.{partition_column} = \
         {reference}.{REPLAY_PARTITION}\n"
    )
}

fn offset_lower_bound_clause(
    source_alias: &str,
    offset_column: &str,
    has_lower_bound: bool,
    inclusive: bool,
    lower_alias: Option<&str>,
) -> String {
    if !has_lower_bound {
        return String::new();
    }
    let reference = lower_alias.unwrap_or(ACTIVE_START_OFFSETS);
    format!(
        "  AND ({reference}.start_offset IS NULL OR {source_alias}.{offset_column} {} \
         {reference}.start_offset)\n",
        if inclusive { ">=" } else { ">" }
    )
}

fn offset_upper_bound_clause(source_alias: &str, offset_column: &str) -> String {
    format!(
        "((cutoff_offsets.cutoff_inclusive AND {source_alias}.{offset_column} <= \
         cutoff_offsets.cutoff_offset) OR (NOT cutoff_offsets.cutoff_inclusive AND \
         {source_alias}.{offset_column} < cutoff_offsets.cutoff_offset))"
    )
}

fn join<T: AsRef<str>>(items: impl Iterator<Item = T>, separator: &str) -> String {
    items
        .map(|item| item.as_ref().to_owned())
        .collect::<Vec<_>>()
        .join(separator)
}
