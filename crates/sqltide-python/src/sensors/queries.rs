//! Exact SQL for sensor state and observation stream reads.

const NODE_RESULTS_TABLE: &str = "_sqltide_node_results";
const INVOCATIONS_TABLE: &str = "_sqltide_invocations";
const CHECKPOINTS_TABLE: &str = "_sqltide_sensor_checkpoints";
const TICKS_TABLE: &str = "_sqltide_sensor_ticks";
const STEPS_TABLE: &str = "_sqltide_sensor_steps";
const OVERRIDES_TABLE: &str = "_sqltide_sensor_overrides";
const LEASES_TABLE: &str = "_sqltide_sensor_leases";
const NODE_RESULT_COLUMNS: &str = "result_id, invocation_id, node_kind, node_name, binding_key, \
     target_identity, trigger, status, severity, failure_count, completed_at, scheduled_for, \
     payload_json, error_message";
const INVOCATION_COLUMNS: &str = "invocation_id, command, mode, outcome, exit_code, \
     target_identity, deployment_id, selected_node_count, error_message, completed_at";
const TICK_COLUMNS: &str = "tick_id, sensor_name, definition_fingerprint, kind, event_id, \
     event_kind, attempt, status, started_at, completed_at, error_message, skip_reason, cursor";

/// A single-quoted ClickHouse string literal.
pub(crate) fn sql_literal(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
}

fn timestamp_literal(value: &str) -> String {
    format!("toDateTime64({}, 3, 'UTC')", sql_literal(value))
}

pub(crate) fn checkpoint(database: &str, sensor_name: &str, source: &str) -> String {
    format!(
        "SELECT position_completed_at, position_result_id FROM {database}.{CHECKPOINTS_TABLE} \
         WHERE sensor_name = {} AND source = {} \
         ORDER BY position_completed_at DESC, position_result_id DESC LIMIT 1",
        sql_literal(sensor_name),
        sql_literal(source)
    )
}

pub(crate) fn newest_node_result_position(database: &str, target: &str) -> String {
    format!(
        "SELECT completed_at, result_id FROM {database}.{NODE_RESULTS_TABLE} \
         WHERE target_identity = {} ORDER BY completed_at DESC, result_id DESC LIMIT 1",
        sql_literal(target)
    )
}

pub(crate) fn newest_invocation_position(database: &str, target: &str) -> String {
    format!(
        "SELECT completed_at, invocation_id FROM {database}.{INVOCATIONS_TABLE} \
         WHERE target_identity = {} ORDER BY completed_at DESC, invocation_id DESC LIMIT 1",
        sql_literal(target)
    )
}

pub(crate) fn node_results_after(
    database: &str,
    target: &str,
    completed_at: &str,
    result_id: &str,
    limit: &str,
) -> String {
    format!(
        "SELECT {NODE_RESULT_COLUMNS} FROM {database}.{NODE_RESULTS_TABLE} \
         WHERE target_identity = {} AND (completed_at, result_id) > ({}, {}) \
         ORDER BY completed_at, result_id LIMIT {limit}",
        sql_literal(target),
        timestamp_literal(completed_at),
        sql_literal(result_id)
    )
}

pub(crate) fn node_result_by_id(database: &str, result_id: &str) -> String {
    format!(
        "SELECT {NODE_RESULT_COLUMNS} FROM {database}.{NODE_RESULTS_TABLE} \
         WHERE result_id = {} ORDER BY completed_at DESC LIMIT 1",
        sql_literal(result_id)
    )
}

pub(crate) fn invocations_after(
    database: &str,
    target: &str,
    completed_at: &str,
    invocation_id: &str,
    limit: &str,
) -> String {
    format!(
        "SELECT {INVOCATION_COLUMNS} FROM {database}.{INVOCATIONS_TABLE} \
         WHERE target_identity = {} AND (completed_at, invocation_id) > ({}, {}) \
         ORDER BY completed_at, invocation_id LIMIT {limit}",
        sql_literal(target),
        timestamp_literal(completed_at),
        sql_literal(invocation_id)
    )
}

pub(crate) fn invocation_by_id(database: &str, invocation_id: &str) -> String {
    format!(
        "SELECT {INVOCATION_COLUMNS} FROM {database}.{INVOCATIONS_TABLE} \
         WHERE invocation_id = {} ORDER BY completed_at DESC LIMIT 1",
        sql_literal(invocation_id)
    )
}

pub(crate) fn previous_node_status(
    database: &str,
    target: &str,
    binding_key: &str,
    completed_at: &str,
    result_id: &str,
) -> String {
    format!(
        "SELECT status FROM {database}.{NODE_RESULTS_TABLE} \
         WHERE target_identity = {} AND binding_key = {} AND status != 'deferred' \
         AND (completed_at, result_id) < ({}, {}) \
         ORDER BY completed_at DESC, result_id DESC LIMIT 1",
        sql_literal(target),
        sql_literal(binding_key),
        timestamp_literal(completed_at),
        sql_literal(result_id)
    )
}

pub(crate) fn event_ticks(database: &str, sensor_name: &str, event_id: &str) -> String {
    format!(
        "SELECT {TICK_COLUMNS} FROM {database}.{TICKS_TABLE} \
         WHERE sensor_name = {} AND event_id = {} ORDER BY started_at, tick_id",
        sql_literal(sensor_name),
        sql_literal(event_id)
    )
}

pub(crate) fn sensor_ticks(
    database: &str,
    sensor_name: &str,
    limit: &str,
    after: Option<&str>,
    before: Option<&str>,
) -> String {
    let after: String = after.map_or_else(String::new, |after| {
        format!("AND started_at >= {} ", timestamp_literal(after))
    });
    let before: String = before.map_or_else(String::new, |before| {
        format!("AND started_at <= {} ", timestamp_literal(before))
    });
    format!(
        "SELECT {TICK_COLUMNS} FROM {database}.{TICKS_TABLE} WHERE sensor_name = {} \
         {after}{before}ORDER BY started_at DESC, tick_id DESC LIMIT {limit}",
        sql_literal(sensor_name)
    )
}

pub(crate) fn dead_letter_candidate_ticks(database: &str) -> String {
    format!(
        "SELECT {TICK_COLUMNS} FROM {database}.{TICKS_TABLE} \
         WHERE (sensor_name, ifNull(event_id, '')) IN (\
         SELECT sensor_name, ifNull(event_id, '') FROM {database}.{TICKS_TABLE} \
         WHERE status = 'dead_lettered') ORDER BY started_at, tick_id"
    )
}

pub(crate) fn latest_polling_tick_start(database: &str, sensor_name: &str) -> String {
    format!(
        "SELECT started_at FROM {database}.{TICKS_TABLE} WHERE sensor_name = {} \
         ORDER BY started_at DESC, tick_id DESC LIMIT 1",
        sql_literal(sensor_name)
    )
}

pub(crate) fn latest_polling_success(database: &str, sensor_name: &str) -> String {
    format!(
        "SELECT started_at, completed_at, cursor FROM {database}.{TICKS_TABLE} \
         WHERE sensor_name = {} AND status = 'succeeded' \
         ORDER BY started_at DESC, tick_id DESC LIMIT 1",
        sql_literal(sensor_name)
    )
}

pub(crate) fn step_markers(
    database: &str,
    sensor_name: &str,
    event_id: &str,
    step_key: &str,
) -> String {
    format!(
        "SELECT status, result_json, attempt FROM {database}.{STEPS_TABLE} \
         WHERE sensor_name = {} AND event_id = {} AND step_key = {} \
         ORDER BY recorded_at, status",
        sql_literal(sensor_name),
        sql_literal(event_id),
        sql_literal(step_key)
    )
}

pub(crate) fn override_statuses(database: &str) -> String {
    format!(
        "SELECT sensor_name, argMax(status, tuple(changed_at, override_id)) AS status \
         FROM {database}.{OVERRIDES_TABLE} GROUP BY sensor_name"
    )
}

pub(crate) fn current_lease(database: &str, lease_name: &str, now: &str) -> String {
    format!(
        "SELECT owner_id FROM {database}.{LEASES_TABLE} \
         WHERE lease_name = {} AND expires_at > {} \
         ORDER BY acquired_at DESC, expires_at DESC, owner_id DESC LIMIT 1",
        sql_literal(lease_name),
        timestamp_literal(now)
    )
}
