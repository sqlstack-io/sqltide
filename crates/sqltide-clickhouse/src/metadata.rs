//! SQLTide's metadata tables in ClickHouse: the idempotent migration, row inserts for deployment,
//! run, and sensor state, manifest publication, and the queries that read them back.

use sha2::{Digest, Sha256};

const SCHEMA_VERSION: u32 = 7;
const BOUNDARY_PARTITION_PREFIX: &str = "_replay_partition=";
const OBJECT_STATE_KIND_DEPLOYMENT: &str = "deployment";
const NOW: &str = "now64(3, 'UTC')";

const SCHEMA_VERSIONS_TABLE: (&str, &str) = (
    "_sqltide_schema_versions",
    "(\n    version UInt64,\n    applied_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (version)",
);
const OBJECT_STATE_TABLE: (&str, &str) = (
    "_sqltide_virtual_object_state",
    "(\n    state_id String,\n    observation_id String,\n    state_kind LowCardinality(String),\n    deployment_id Nullable(String),\n    logical_database_name Nullable(String),\n    logical_object_type String,\n    logical_object_name String,\n    physical_database_name Nullable(String),\n    physical_relation_name Nullable(String),\n    logical_model_database Nullable(String),\n    logical_model_name Nullable(String),\n    is_selected_root Bool,\n    object_fingerprint String,\n    canonical_query Nullable(String),\n    observed_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (state_kind, state_id, logical_object_type, logical_object_name)",
);
const DEPLOYMENTS_TABLE: (&str, &str) = (
    "_sqltide_virtual_deployments",
    "(\n    deployment_id String,\n    workflow_fingerprint String,\n    replay_lineage_mode String,\n    boundary_time DateTime64(3, 'UTC'),\n    created_at DateTime64(3, 'UTC'),\n    tool_version String\n) ENGINE = MergeTree\nORDER BY (deployment_id)",
);
const DEPLOYMENT_WATERMARKS_TABLE: (&str, &str) = (
    "_sqltide_virtual_replay_boundaries",
    "(\n    deployment_id String,\n    root_database_name Nullable(String),\n    root_object_type String,\n    root_object_name String,\n    anchor_database_name Nullable(String),\n    anchor_object_type String,\n    anchor_object_name String,\n    boundary_kind LowCardinality(String),\n    value_kind LowCardinality(String),\n    partition_value Nullable(String),\n    lower_value Nullable(String),\n    cutoff_value String,\n    cutoff_inclusive Bool,\n    captured_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (deployment_id, root_object_type, root_object_name, boundary_kind, ifNull(partition_value, ''))",
);
const PUBLISH_HISTORY_TABLE: (&str, &str) = (
    "_sqltide_virtual_publications",
    "(\n    publication_id String,\n    deployment_id String,\n    operation LowCardinality(String) DEFAULT 'promote',\n    previous_deployment_id Nullable(String),\n    logical_database_name String,\n    logical_view_name String,\n    physical_database_name String,\n    physical_relation_name String,\n    published_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (publication_id, logical_database_name, logical_view_name)",
);
const DIRECT_FINGERPRINTS_TABLE: (&str, &str) = (
    "_sqltide_direct_fingerprints",
    "(\n    fingerprint_id String,\n    logical_model_identity String,\n    definition_sql String,\n    definition_hash String,\n    identity_metadata String,\n    workflow_id String,\n    tool_version String,\n    applied_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree ORDER BY (logical_model_identity, applied_at, fingerprint_id)",
);
const INVOCATIONS_TABLE: (&str, &str) = (
    "_sqltide_invocations",
    "(\n    invocation_id String,\n    project_identity String,\n    target_identity String,\n    command LowCardinality(String),\n    mode Nullable(String),\n    outcome LowCardinality(String),\n    exit_code Int32,\n    materialized_outcome Nullable(String),\n    deployment_id Nullable(String),\n    workflow_id Nullable(String),\n    selected_node_count UInt64,\n    started_at DateTime64(3, 'UTC'),\n    completed_at DateTime64(3, 'UTC'),\n    duration_ms UInt64,\n    error_message Nullable(String),\n    summary_json String,\n    tool_version String\n) ENGINE = MergeTree\nORDER BY (project_identity, target_identity, completed_at, invocation_id)",
);
const NODE_RESULTS_TABLE: (&str, &str) = (
    "_sqltide_node_results",
    "(\n    result_id String,\n    invocation_id String,\n    node_kind LowCardinality(String),\n    node_name String,\n    binding_key String,\n    definition_fingerprint String,\n    execution_fingerprint String,\n    target_identity String,\n    trigger LowCardinality(String),\n    scheduled_for Nullable(DateTime64(3, 'UTC')),\n    cadence_seconds Nullable(UInt64),\n    warmup_seconds UInt64,\n    status LowCardinality(String),\n    severity Nullable(String),\n    failure_count UInt64,\n    completed_at DateTime64(3, 'UTC'),\n    payload_json String,\n    error_message Nullable(String)\n) ENGINE = MergeTree\nORDER BY (node_kind, node_name, completed_at, result_id)",
);
const RUN_EVENTS_TABLE: (&str, &str) = (
    "_sqltide_run_events",
    "(\n    invocation_id String,\n    sequence UInt64,\n    emitted_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),\n    event_kind LowCardinality(String),\n    step_id Nullable(String),\n    phase Nullable(String),\n    payload_json String\n) ENGINE = MergeTree\nORDER BY (invocation_id, sequence)",
);
const RUN_STATEMENTS_TABLE: (&str, &str) = (
    "_sqltide_run_statements",
    "(\n    invocation_id String,\n    statement_sequence UInt64,\n    step_id String,\n    phase LowCardinality(String),\n    intent LowCardinality(String),\n    sql String,\n    sql_sha256 FixedString(64),\n    workflow_sha256 FixedString(64),\n    workflow_revision UInt64,\n    recorded_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')\n) ENGINE = ReplacingMergeTree(workflow_revision)\nORDER BY (invocation_id, statement_sequence)",
);
const SENSOR_CHECKPOINTS_TABLE: (&str, &str) = (
    "_sqltide_sensor_checkpoints",
    "(\n    sensor_name String,\n    source LowCardinality(String),\n    position_completed_at DateTime64(3, 'UTC'),\n    position_result_id String,\n    recorded_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (sensor_name, source, position_completed_at, position_result_id)",
);
const SENSOR_TICKS_TABLE: (&str, &str) = (
    "_sqltide_sensor_ticks",
    "(\n    tick_id String,\n    sensor_name String,\n    definition_fingerprint String,\n    kind LowCardinality(String),\n    event_id Nullable(String),\n    event_kind Nullable(String),\n    attempt UInt32,\n    status LowCardinality(String),\n    started_at DateTime64(3, 'UTC'),\n    completed_at Nullable(DateTime64(3, 'UTC')),\n    error_message Nullable(String),\n    skip_reason Nullable(String),\n    cursor Nullable(String)\n) ENGINE = MergeTree\nORDER BY (sensor_name, started_at, tick_id)",
);
const SENSOR_STEPS_TABLE: (&str, &str) = (
    "_sqltide_sensor_steps",
    "(\n    sensor_name String,\n    event_id String,\n    step_key String,\n    policy LowCardinality(String),\n    status LowCardinality(String),\n    attempt UInt32,\n    result_json Nullable(String),\n    error_message Nullable(String),\n    recorded_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (sensor_name, event_id, step_key, recorded_at)",
);
const SENSOR_OVERRIDES_TABLE: (&str, &str) = (
    "_sqltide_sensor_overrides",
    "(\n    override_id String,\n    sensor_name String,\n    status LowCardinality(String),\n    actor Nullable(String),\n    changed_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (sensor_name, changed_at, override_id)",
);
const SENSOR_LEASES_TABLE: (&str, &str) = (
    "_sqltide_sensor_leases",
    "(\n    lease_name String,\n    owner_id String,\n    acquired_at DateTime64(3, 'UTC'),\n    expires_at DateTime64(3, 'UTC')\n) ENGINE = MergeTree\nORDER BY (lease_name, acquired_at, owner_id)",
);
const MANIFESTS_TABLE: (&str, &str) = (
    "_sqltide_manifests",
    "(\n    manifest_id String,\n    invocation_id String,\n    project_identity String,\n    target_name String,\n    target_database String,\n    is_production Bool,\n    project_revision Nullable(String),\n    manifest_fingerprint String,\n    manifest_version UInt64,\n    pipelines Array(String),\n    resources Array(Tuple(\n        pipeline_name String,\n        logical_type String,\n        logical_name String,\n        resource_role String,\n        resource_database String,\n        resource_name String,\n        resource_kind String\n    )),\n    tool_version String,\n    published_at DateTime64(6, 'UTC')\n) ENGINE = MergeTree\nORDER BY (project_identity, target_name, target_database, published_at, manifest_id)",
);
const LATEST_NODE_STATUS_QUERY: &str = "WITH manifest_nodes AS ({manifest_sql}), logical_snapshots AS (SELECT result.node_kind AS node_kind, result.node_name AS node_name, argMax(tuple(result.binding_key, result.definition_fingerprint, result.execution_fingerprint, result.status, result.severity, result.failure_count, result.completed_at, result.payload_json, result.error_message, result.cadence_seconds, result.warmup_seconds, result.result_id), tuple(result.completed_at, result.result_id)) AS snapshot FROM {database}._sqltide_node_results AS result INNER JOIN {database}._sqltide_invocations AS invocation ON invocation.invocation_id = result.invocation_id WHERE result.target_identity = {target} AND (invocation.project_identity = {project} OR endsWith(invocation.project_identity, {project_suffix})) GROUP BY result.node_kind, result.node_name, result.binding_key, result.execution_fingerprint, ifNull(toString(result.scheduled_for), result.result_id)), logical_results AS (SELECT node_kind, node_name, tupleElement(snapshot, 1) AS binding_key, tupleElement(snapshot, 2) AS definition_fingerprint, tupleElement(snapshot, 3) AS execution_fingerprint, tupleElement(snapshot, 4) AS status, tupleElement(snapshot, 5) AS severity, tupleElement(snapshot, 6) AS failure_count, tupleElement(snapshot, 7) AS completed_at, tupleElement(snapshot, 8) AS payload_json, tupleElement(snapshot, 9) AS error_message, tupleElement(snapshot, 10) AS cadence_seconds, tupleElement(snapshot, 11) AS warmup_seconds, tupleElement(snapshot, 12) AS result_id FROM logical_snapshots), latest_results AS (SELECT node_kind, node_name, tupleElement(snapshot, 1) AS binding_key, tupleElement(snapshot, 2) AS definition_fingerprint, tupleElement(snapshot, 3) AS execution_fingerprint, tupleElement(snapshot, 4) AS status, tupleElement(snapshot, 5) AS severity, tupleElement(snapshot, 6) AS failure_count, tupleElement(snapshot, 7) AS completed_at, tupleElement(snapshot, 8) AS payload_json, tupleElement(snapshot, 9) AS error_message, tupleElement(snapshot, 10) AS cadence_seconds, tupleElement(snapshot, 11) AS warmup_seconds, tupleElement(snapshot, 12) AS result_id FROM (SELECT node_kind, node_name, argMax(tuple(binding_key, definition_fingerprint, execution_fingerprint, status, severity, failure_count, completed_at, payload_json, error_message, cadence_seconds, warmup_seconds, result_id), tuple(completed_at, result_id)) AS snapshot FROM logical_results GROUP BY node_kind, node_name)), matching_results AS (SELECT node_kind, node_name, tupleElement(snapshot, 1) AS binding_key, tupleElement(snapshot, 2) AS definition_fingerprint, tupleElement(snapshot, 3) AS execution_fingerprint, tupleElement(snapshot, 4) AS status, tupleElement(snapshot, 5) AS severity, tupleElement(snapshot, 6) AS failure_count, tupleElement(snapshot, 7) AS completed_at, tupleElement(snapshot, 8) AS payload_json, tupleElement(snapshot, 9) AS error_message, tupleElement(snapshot, 10) AS cadence_seconds, tupleElement(snapshot, 11) AS warmup_seconds, tupleElement(snapshot, 12) AS result_id FROM (SELECT logical.node_kind AS node_kind, logical.node_name AS node_name, argMax(tuple(logical.binding_key, logical.definition_fingerprint, logical.execution_fingerprint, logical.status, logical.severity, logical.failure_count, logical.completed_at, logical.payload_json, logical.error_message, logical.cadence_seconds, logical.warmup_seconds, logical.result_id), tuple(logical.completed_at, logical.result_id)) AS snapshot FROM logical_results AS logical INNER JOIN manifest_nodes AS manifest ON logical.node_kind = manifest.node_kind AND logical.node_name = manifest.node_name WHERE logical.binding_key = manifest.binding_key AND logical.definition_fingerprint = manifest.definition_fingerprint AND logical.execution_fingerprint = manifest.execution_fingerprint GROUP BY logical.node_kind, logical.node_name)) SELECT manifest.node_kind AS node_kind, manifest.node_name AS node_name, manifest.binding_key AS binding_key, manifest.definition_fingerprint AS definition_fingerprint, manifest.execution_fingerprint AS execution_fingerprint, manifest.cadence_seconds AS cadence_seconds, manifest.warmup_seconds AS warmup_seconds, multiIf(latest.node_name = '', 'never_run', matching.node_name != '' AND (ifNull(matching.cadence_seconds, 0) != ifNull(manifest.cadence_seconds, 0) OR matching.warmup_seconds != manifest.warmup_seconds), 'schedule_changed', matching.node_name != '', matching.status, latest.binding_key != manifest.binding_key, 'binding_changed', latest.definition_fingerprint != manifest.definition_fingerprint, 'definition_changed', latest.execution_fingerprint != manifest.execution_fingerprint, 'execution_changed', ifNull(latest.cadence_seconds, 0) != ifNull(manifest.cadence_seconds, 0) OR latest.warmup_seconds != manifest.warmup_seconds, 'schedule_changed', latest.status) AS current_status, arrayFilter(reason -> reason != '', [if(matching.node_name = '' AND latest.node_name != '' AND latest.binding_key != manifest.binding_key, 'binding_changed', ''), if(matching.node_name = '' AND latest.node_name != '' AND latest.definition_fingerprint != manifest.definition_fingerprint, 'definition_changed', ''), if(matching.node_name = '' AND latest.node_name != '' AND latest.execution_fingerprint != manifest.execution_fingerprint, 'execution_changed', ''), if(ifNull(matching.cadence_seconds, ifNull(latest.cadence_seconds, 0)) != ifNull(manifest.cadence_seconds, 0) OR ifNull(matching.warmup_seconds, latest.warmup_seconds) != manifest.warmup_seconds, 'schedule_changed', '')]) AS drift_reasons, nullIf(if(matching.node_name != '', matching.severity, latest.severity), '') AS severity, if(matching.node_name != '', matching.failure_count, latest.failure_count) AS failure_count, if(matching.node_name != '', matching.completed_at, latest.completed_at) AS completed_at, if(matching.node_name != '', matching.payload_json, latest.payload_json) AS payload_json, nullIf(if(matching.node_name != '', matching.error_message, latest.error_message), '') AS error_message FROM manifest_nodes AS manifest LEFT JOIN latest_results AS latest ON latest.node_kind = manifest.node_kind AND latest.node_name = manifest.node_name LEFT JOIN matching_results AS matching ON matching.node_kind = manifest.node_kind AND matching.node_name = manifest.node_name ORDER BY manifest.node_kind, manifest.node_name";
const EMPTY_MANIFEST_NODES: &str = "SELECT CAST('' AS String) AS node_kind, CAST('' AS String) AS node_name, CAST('' AS String) AS binding_key, CAST('' AS String) AS definition_fingerprint, CAST('' AS String) AS execution_fingerprint, CAST(NULL AS Nullable(UInt64)) AS cadence_seconds, CAST(0 AS UInt64) AS warmup_seconds WHERE false";

/// One SQL literal value of a metadata row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Literal {
    Null,
    Bool(bool),
    Integer(i64),
    /// A number already written as SQL, such as a float in its host language's notation.
    Number(String),
    Text(String),
}

impl From<&str> for Literal {
    fn from(value: &str) -> Self {
        Self::Text(value.to_owned())
    }
}

impl From<&String> for Literal {
    fn from(value: &String) -> Self {
        Self::Text(value.clone())
    }
}

impl From<&Option<String>> for Literal {
    fn from(value: &Option<String>) -> Self {
        value
            .as_ref()
            .map_or(Self::Null, |text| Self::Text(text.clone()))
    }
}

impl From<i64> for Literal {
    fn from(value: i64) -> Self {
        Self::Integer(value)
    }
}

impl From<Option<i64>> for Literal {
    fn from(value: Option<i64>) -> Self {
        value.map_or(Self::Null, Self::Integer)
    }
}

impl From<bool> for Literal {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

/// Render one value as a ClickHouse literal, escaping backslashes and single quotes.
#[must_use]
pub fn literal(value: &Literal) -> String {
    match value {
        Literal::Null => "NULL".to_owned(),
        Literal::Bool(flag) => if *flag { "true" } else { "false" }.to_owned(),
        Literal::Integer(number) => number.to_string(),
        Literal::Number(number) => number.clone(),
        Literal::Text(text) => text_literal(text),
    }
}

fn text_literal(text: &str) -> String {
    format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectKey {
    pub database: Option<String>,
    pub object_type: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObjectState {
    pub deployment_id: String,
    pub key: ObjectKey,
    pub normalized_fingerprint: String,
    pub normalized_query: Option<String>,
    pub recorded_at: String,
    pub observation_id: String,
    pub state_kind: String,
    pub physical_database_name: Option<String>,
    pub physical_relation_name: Option<String>,
    pub logical_model_database: Option<String>,
    pub logical_model_name: Option<String>,
    pub is_selected_root: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Deployment {
    pub deployment_id: String,
    pub workflow_fingerprint: String,
    pub replay_lineage_mode: String,
    pub boundary_time: Option<String>,
    pub created_at: String,
    pub tool_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Watermark {
    pub deployment_id: String,
    pub root_key: ObjectKey,
    pub anchor_key: ObjectKey,
    pub boundary_key: String,
    pub cutoff_value: String,
    pub lower_value: Option<String>,
    pub cutoff_inclusive: bool,
    pub captured_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub database: String,
    pub logical_name: String,
    pub physical_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishEvent {
    pub deployment_id: String,
    pub published_at: String,
    pub publication_id: String,
    pub bindings: Vec<Binding>,
    pub operation: String,
    pub previous_deployment_id: Option<String>,
}

/// One metadata table's insert header and its rows, in column order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rows {
    pub header: String,
    pub rows: Vec<Vec<Literal>>,
}

/// Every metadata record of one state, as rows ready to insert.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MetadataState {
    pub object_states: Vec<ObjectState>,
    pub deployments: Vec<Deployment>,
    pub watermarks: Vec<Watermark>,
    pub publish_events: Vec<PublishEvent>,
    pub invocations: Vec<Vec<Literal>>,
    pub node_results: Vec<Vec<Literal>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirectFingerprint {
    pub fingerprint_id: String,
    pub logical_model_identity: String,
    pub definition_sql: String,
    pub definition_hash: String,
    pub identity_metadata: String,
    pub workflow_id: String,
    pub tool_version: String,
    pub applied_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub manifest_id: String,
    pub invocation_id: String,
    pub project_identity: String,
    pub target_name: String,
    pub target_database: String,
    pub is_production: bool,
    pub project_revision: Option<String>,
    pub manifest_fingerprint: String,
    pub manifest_version: i64,
    pub pipelines: Vec<String>,
    /// `(pipeline, logical type, logical name, role, database, name, kind)` per resource.
    pub resources: Vec<[String; 7]>,
    pub tool_version: String,
    pub published_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QualityNode {
    pub node_kind: String,
    pub node_name: String,
    pub binding_key: String,
    pub definition_fingerprint: String,
    pub execution_fingerprint: String,
    pub cadence_seconds: Option<i64>,
    pub warmup_seconds: i64,
}

fn create_table(database: &str, (name, body): (&str, &str)) -> String {
    format!("CREATE TABLE IF NOT EXISTS {database}.{name} {body}")
}

fn terminated(statement: &str) -> String {
    format!("{};", statement.trim_end().trim_end_matches(';'))
}

/// The current additive metadata migration, unterminated.
#[must_use]
pub fn migration_statements(database: &str) -> Vec<String> {
    let mut statements: Vec<String> = [
        OBJECT_STATE_TABLE,
        DEPLOYMENTS_TABLE,
        DEPLOYMENT_WATERMARKS_TABLE,
        PUBLISH_HISTORY_TABLE,
        DIRECT_FINGERPRINTS_TABLE,
        INVOCATIONS_TABLE,
        NODE_RESULTS_TABLE,
        RUN_EVENTS_TABLE,
        RUN_STATEMENTS_TABLE,
        SENSOR_CHECKPOINTS_TABLE,
        SENSOR_TICKS_TABLE,
        SENSOR_STEPS_TABLE,
        SENSOR_OVERRIDES_TABLE,
        SENSOR_LEASES_TABLE,
        MANIFESTS_TABLE,
    ]
    .into_iter()
    .map(|table| create_table(database, table))
    .collect();
    statements.push(format!(
        "DROP TABLE IF EXISTS {database}._sqltide_owned_resources SYNC"
    ));
    statements.push(format!(
        "ALTER TABLE {database}.{} ADD COLUMN IF NOT EXISTS operation LowCardinality(String) \
         DEFAULT 'promote' AFTER deployment_id, ADD COLUMN IF NOT EXISTS previous_deployment_id \
         Nullable(String) AFTER operation",
        PUBLISH_HISTORY_TABLE.0
    ));
    statements
}

/// The complete idempotent metadata migration as executable SQL.
#[must_use]
pub fn migration_workflow(database: &str) -> Vec<String> {
    let versions = SCHEMA_VERSIONS_TABLE.0;
    let mut statements: Vec<String> = vec![
        format!("CREATE DATABASE IF NOT EXISTS {database};"),
        terminated(&create_table(database, SCHEMA_VERSIONS_TABLE)),
    ];
    statements.extend(
        migration_statements(database)
            .iter()
            .map(|statement| terminated(statement)),
    );
    statements.push(format!(
        "INSERT INTO {database}.{versions} (version, applied_at) SELECT {SCHEMA_VERSION}, {NOW} \
         WHERE NOT EXISTS (SELECT 1 FROM {database}.{versions} WHERE version = {SCHEMA_VERSION});"
    ));
    statements
}

fn insert(rows: &Rows) -> Option<String> {
    if rows.rows.is_empty() {
        return None;
    }
    let values: Vec<String> = rows
        .rows
        .iter()
        .map(|row| {
            format!(
                "({})",
                row.iter().map(literal).collect::<Vec<_>>().join(", ")
            )
        })
        .collect();
    Some(format!("{}\n{};", rows.header, values.join(",\n")))
}

fn header(database: &str, table: &str, columns: &str) -> String {
    format!("INSERT INTO {database}.{table} ({columns}) VALUES")
}

/// Exact, manually executable inserts for every non-empty metadata table of `state`.
#[must_use]
pub fn metadata_state(database: &str, state: &MetadataState) -> Vec<String> {
    let tables: [Rows; 6] = [
        Rows {
            header: header(
                database,
                OBJECT_STATE_TABLE.0,
                "state_id, observation_id, state_kind, deployment_id, logical_database_name, \
                 logical_object_type, logical_object_name, physical_database_name, \
                 physical_relation_name, logical_model_database, logical_model_name, \
                 is_selected_root, object_fingerprint, canonical_query, observed_at",
            ),
            rows: state.object_states.iter().map(object_state_row).collect(),
        },
        Rows {
            header: header(
                database,
                DEPLOYMENTS_TABLE.0,
                "deployment_id, workflow_fingerprint, replay_lineage_mode, boundary_time, \
                 created_at, tool_version",
            ),
            rows: state
                .deployments
                .iter()
                .filter(|deployment| deployment.boundary_time.is_some())
                .map(|deployment| {
                    vec![
                        (&deployment.deployment_id).into(),
                        (&deployment.workflow_fingerprint).into(),
                        (&deployment.replay_lineage_mode).into(),
                        (&deployment.boundary_time).into(),
                        (&deployment.created_at).into(),
                        (&deployment.tool_version).into(),
                    ]
                })
                .collect(),
        },
        Rows {
            header: header(
                database,
                DEPLOYMENT_WATERMARKS_TABLE.0,
                "deployment_id, root_database_name, root_object_type, root_object_name, \
                 anchor_database_name, anchor_object_type, anchor_object_name, boundary_kind, \
                 value_kind, partition_value, lower_value, cutoff_value, cutoff_inclusive, \
                 captured_at",
            ),
            rows: state.watermarks.iter().map(watermark_row).collect(),
        },
        Rows {
            header: header(
                database,
                PUBLISH_HISTORY_TABLE.0,
                "publication_id, deployment_id, operation, previous_deployment_id, \
                 logical_database_name, logical_view_name, physical_database_name, \
                 physical_relation_name, published_at",
            ),
            rows: state.publish_events.iter().flat_map(publish_rows).collect(),
        },
        Rows {
            header: header(
                database,
                INVOCATIONS_TABLE.0,
                "invocation_id, project_identity, target_identity, command, mode, outcome, \
                 exit_code, materialized_outcome, deployment_id, workflow_id, \
                 selected_node_count, started_at, completed_at, duration_ms, error_message, \
                 summary_json, tool_version",
            ),
            rows: state.invocations.clone(),
        },
        Rows {
            header: header(
                database,
                NODE_RESULTS_TABLE.0,
                "result_id, invocation_id, node_kind, node_name, binding_key, \
                 definition_fingerprint, execution_fingerprint, target_identity, trigger, \
                 scheduled_for, cadence_seconds, warmup_seconds, status, severity, \
                 failure_count, completed_at, payload_json, error_message",
            ),
            rows: state.node_results.clone(),
        },
    ];
    tables.iter().filter_map(insert).collect()
}

/// Python's `json.dumps` string encoding with its default `ensure_ascii`.
fn json_string(text: &str) -> String {
    let mut encoded = String::with_capacity(text.len() + 2);
    encoded.push('"');
    for character in text.chars() {
        match character {
            '"' => encoded.push_str("\\\""),
            '\\' => encoded.push_str("\\\\"),
            '\n' => encoded.push_str("\\n"),
            '\r' => encoded.push_str("\\r"),
            '\t' => encoded.push_str("\\t"),
            '\u{8}' => encoded.push_str("\\b"),
            '\u{c}' => encoded.push_str("\\f"),
            ' '..='~' => encoded.push(character),
            _ => {
                let mut units = [0_u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    encoded.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    encoded.push('"');
    encoded
}

fn json_optional(value: Option<&str>) -> String {
    value.map_or_else(|| "null".to_owned(), json_string)
}

fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn object_state_row(record: &ObjectState) -> Vec<Literal> {
    let observation_id = if record.observation_id.is_empty() {
        let fields: [(&str, Option<&str>); 10] = [
            ("canonical_query", record.normalized_query.as_deref()),
            ("logical_database_name", record.key.database.as_deref()),
            ("logical_object_name", Some(&record.key.name)),
            ("logical_object_type", Some(&record.key.object_type)),
            ("object_fingerprint", Some(&record.normalized_fingerprint)),
            ("observed_at", Some(&record.recorded_at)),
            (
                "physical_database_name",
                record.physical_database_name.as_deref(),
            ),
            (
                "physical_relation_name",
                record.physical_relation_name.as_deref(),
            ),
            ("state_id", Some(&record.deployment_id)),
            ("state_kind", Some(&record.state_kind)),
        ];
        let entries: Vec<String> = fields
            .iter()
            .map(|(name, value)| format!("{}:{}", json_string(name), json_optional(*value)))
            .collect();
        sha256_hex(&format!("{{{}}}", entries.join(",")))
    } else {
        record.observation_id.clone()
    };
    let deployment_id: Option<String> =
        (record.state_kind == OBJECT_STATE_KIND_DEPLOYMENT).then(|| record.deployment_id.clone());
    vec![
        (&record.deployment_id).into(),
        (&observation_id).into(),
        (&record.state_kind).into(),
        (&deployment_id).into(),
        (&record.key.database).into(),
        (&record.key.object_type).into(),
        (&record.key.name).into(),
        (&record.physical_database_name).into(),
        (&record.physical_relation_name).into(),
        (&record.logical_model_database).into(),
        (&record.logical_model_name).into(),
        record.is_selected_root.into(),
        (&record.normalized_fingerprint).into(),
        (&record.normalized_query).into(),
        (&record.recorded_at).into(),
    ]
}

fn boundary_kind(boundary_key: &str) -> &str {
    if boundary_key.starts_with(BOUNDARY_PARTITION_PREFIX) {
        return "offsets";
    }
    boundary_key
        .strip_prefix("_replay_")
        .unwrap_or(boundary_key)
}

fn watermark_row(record: &Watermark) -> Vec<Literal> {
    let kind = boundary_kind(&record.boundary_key);
    let value_kind = if kind == "offsets" || kind == "cursor" {
        "integer"
    } else {
        "timestamp"
    };
    let partition: Option<String> = record
        .boundary_key
        .split_once('=')
        .map(|(_, partition)| partition.to_owned());
    vec![
        (&record.deployment_id).into(),
        (&record.root_key.database).into(),
        (&record.root_key.object_type).into(),
        (&record.root_key.name).into(),
        (&record.anchor_key.database).into(),
        (&record.anchor_key.object_type).into(),
        (&record.anchor_key.name).into(),
        kind.into(),
        value_kind.into(),
        (&partition).into(),
        (&record.lower_value).into(),
        (&record.cutoff_value).into(),
        record.cutoff_inclusive.into(),
        (&record.captured_at).into(),
    ]
}

fn publish_rows(record: &PublishEvent) -> Vec<Vec<Literal>> {
    let publication_id = if record.publication_id.is_empty() {
        let bindings: Vec<String> = record
            .bindings
            .iter()
            .map(|binding| {
                format!(
                    "[{},{},{}]",
                    json_string(&binding.database),
                    json_string(&binding.logical_name),
                    json_string(&binding.physical_name)
                )
            })
            .collect();
        sha256_hex(&format!(
            "[{},{},{},{},[{}]]",
            json_string(&record.deployment_id),
            json_string(&record.published_at),
            json_string(&record.operation),
            json_optional(record.previous_deployment_id.as_deref()),
            bindings.join(",")
        ))
    } else {
        record.publication_id.clone()
    };
    record
        .bindings
        .iter()
        .map(|binding| {
            vec![
                (&publication_id).into(),
                (&record.deployment_id).into(),
                (&record.operation).into(),
                (&record.previous_deployment_id).into(),
                (&binding.database).into(),
                (&binding.logical_name).into(),
                (&binding.database).into(),
                (&binding.physical_name).into(),
                (&record.published_at).into(),
            ]
        })
        .collect()
}

/// The latest compatible direct SQL baseline per logical model identity.
#[must_use]
pub fn latest_fingerprints_query(database: &str, logical_model_identities: &[String]) -> String {
    let identities: Vec<String> = logical_model_identities
        .iter()
        .map(|identity| text_literal(identity))
        .collect();
    format!(
        "SELECT tuple_value.1, logical_model_identity, tuple_value.2, tuple_value.3, \
         tuple_value.4, tuple_value.5, tuple_value.6, tuple_value.7 FROM (SELECT \
         logical_model_identity, argMax(tuple(fingerprint_id, definition_sql, definition_hash, \
         identity_metadata, workflow_id, tool_version, applied_at), tuple(applied_at, \
         fingerprint_id)) AS tuple_value FROM {database}.{} WHERE logical_model_identity IN ({}) \
         GROUP BY logical_model_identity) ORDER BY logical_model_identity",
        DIRECT_FINGERPRINTS_TABLE.0,
        identities.join(", ")
    )
}

/// Best-effort schema initialization and logical baseline inserts.
#[must_use]
pub fn direct_fingerprint_observations(
    database: &str,
    fingerprints: &[DirectFingerprint],
) -> Vec<String> {
    if fingerprints.is_empty() {
        return Vec::new();
    }
    let values: Vec<String> = fingerprints
        .iter()
        .map(|record| {
            let quoted: Vec<String> = [
                &record.fingerprint_id,
                &record.logical_model_identity,
                &record.definition_sql,
                &record.definition_hash,
                &record.identity_metadata,
                &record.workflow_id,
                &record.tool_version,
            ]
            .iter()
            .map(|value| text_literal(value))
            .collect();
            let applied_at = record.applied_at.as_ref().map_or_else(
                || NOW.to_owned(),
                |applied_at| format!("toDateTime64({}, 3, 'UTC')", text_literal(applied_at)),
            );
            format!("({}, {applied_at})", quoted.join(", "))
        })
        .collect();
    vec![
        format!("CREATE DATABASE IF NOT EXISTS {database};"),
        terminated(&create_table(database, DIRECT_FINGERPRINTS_TABLE)),
        format!(
            "{}\n{};",
            header(
                database,
                DIRECT_FINGERPRINTS_TABLE.0,
                "fingerprint_id, logical_model_identity, definition_sql, definition_hash, \
                 identity_metadata, workflow_id, tool_version, applied_at"
            ),
            values.join(",\n")
        ),
    ]
}

/// Complete manifest history for one project target, newest first.
#[must_use]
pub fn manifest_history_query(
    database: &str,
    project_identity: &str,
    target_name: &str,
    target_database: &str,
) -> String {
    format!(
        "SELECT manifest.manifest_id, manifest.invocation_id, manifest.project_identity, \
         manifest.target_name, manifest.target_database, manifest.is_production, \
         manifest.project_revision, manifest.manifest_fingerprint, manifest.manifest_version, \
         manifest.pipelines, manifest.resources, manifest.tool_version, manifest.published_at \
         FROM {database}.{} AS manifest WHERE manifest.project_identity = {} AND \
         manifest.target_name = {} AND manifest.target_database = {} ORDER BY \
         manifest.published_at DESC, manifest.manifest_id DESC",
        MANIFESTS_TABLE.0,
        text_literal(project_identity),
        text_literal(target_name),
        text_literal(target_database)
    )
}

/// One append containing a complete immutable manifest.
#[must_use]
pub fn manifest_publication(database: &str, manifest: &Manifest) -> String {
    let strings: Vec<String> = [
        &manifest.manifest_id,
        &manifest.invocation_id,
        &manifest.project_identity,
        &manifest.target_name,
        &manifest.target_database,
    ]
    .iter()
    .map(|value| text_literal(value))
    .collect();
    let pipelines: Vec<String> = manifest
        .pipelines
        .iter()
        .map(|pipeline| text_literal(pipeline))
        .collect();
    let resources: Vec<String> = manifest
        .resources
        .iter()
        .map(|resource| {
            let literals: Vec<String> = resource.iter().map(|value| text_literal(value)).collect();
            format!("({})", literals.join(", "))
        })
        .collect();
    let revision = manifest
        .project_revision
        .as_ref()
        .map_or_else(|| "NULL".to_owned(), |revision| text_literal(revision));
    format!(
        "INSERT INTO {database}.{} (manifest_id, invocation_id, project_identity, target_name, \
         target_database, is_production, project_revision, manifest_fingerprint, \
         manifest_version, pipelines, resources, tool_version, published_at) VALUES\n({}, {}, \
         {revision}, {}, {}, [{}], [{}], {}, toDateTime64({}, 6, 'UTC'));",
        MANIFESTS_TABLE.0,
        strings.join(", "),
        manifest.is_production,
        text_literal(&manifest.manifest_fingerprint),
        manifest.manifest_version,
        pipelines.join(", "),
        resources.join(", "),
        text_literal(&manifest.tool_version),
        text_literal(&manifest.published_at)
    )
}

/// Which incremental inserts to render, each as rows in column order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SensorState {
    pub checkpoints: Vec<Vec<Literal>>,
    pub ticks: Vec<Vec<Literal>>,
    pub steps: Vec<Vec<Literal>>,
    pub overrides: Vec<Vec<Literal>>,
    pub leases: Vec<Vec<Literal>>,
}

fn with_migration(database: &str, inserts: Vec<String>, include_migration: bool) -> Vec<String> {
    if !include_migration {
        return inserts;
    }
    let mut statements = migration_workflow(database);
    statements.extend(inserts);
    statements
}

/// Incremental sensor-state inserts, optionally preceded by the migration.
#[must_use]
pub fn sensor_state_inserts(
    database: &str,
    state: &SensorState,
    include_migration: bool,
) -> Vec<String> {
    let tables = [
        (
            SENSOR_CHECKPOINTS_TABLE.0,
            "sensor_name, source, position_completed_at, position_result_id",
            &state.checkpoints,
        ),
        (
            SENSOR_TICKS_TABLE.0,
            "tick_id, sensor_name, definition_fingerprint, kind, event_id, event_kind, attempt, \
             status, started_at, completed_at, error_message, skip_reason, cursor",
            &state.ticks,
        ),
        (
            SENSOR_STEPS_TABLE.0,
            "sensor_name, event_id, step_key, policy, status, attempt, result_json, error_message",
            &state.steps,
        ),
        (
            SENSOR_OVERRIDES_TABLE.0,
            "override_id, sensor_name, status, actor",
            &state.overrides,
        ),
        (
            SENSOR_LEASES_TABLE.0,
            "lease_name, owner_id, acquired_at, expires_at",
            &state.leases,
        ),
    ];
    let inserts: Vec<String> = tables
        .into_iter()
        .filter_map(|(table, columns, rows)| {
            insert(&Rows {
                header: header(database, table, columns),
                rows: rows.clone(),
            })
        })
        .collect();
    with_migration(database, inserts, include_migration)
}

/// Bounded deletes for aged sensor ticks and steps; zero or fewer days keeps everything.
#[must_use]
pub fn sensor_retention_cleanup(database: &str, retention_days: i64) -> Vec<String> {
    if retention_days <= 0 {
        return Vec::new();
    }
    let horizon = format!("{NOW} - INTERVAL {retention_days} DAY");
    vec![
        format!(
            "ALTER TABLE {database}.{} DELETE WHERE started_at < {horizon};",
            SENSOR_TICKS_TABLE.0
        ),
        format!(
            "ALTER TABLE {database}.{} DELETE WHERE recorded_at < {horizon};",
            SENSOR_STEPS_TABLE.0
        ),
    ]
}

/// Incremental run-event inserts, optionally preceded by the migration.
#[must_use]
pub fn run_event_inserts(
    database: &str,
    rows: Vec<Vec<Literal>>,
    include_migration: bool,
) -> Vec<String> {
    let Some(statement) = insert(&Rows {
        header: header(
            database,
            RUN_EVENTS_TABLE.0,
            "invocation_id, sequence, event_kind, step_id, phase, payload_json",
        ),
        rows,
    }) else {
        return Vec::new();
    };
    with_migration(database, vec![statement], include_migration)
}

/// One complete run statement set, optionally preceded by the migration.
#[must_use]
pub fn run_statement_inserts(
    database: &str,
    rows: Vec<Vec<Literal>>,
    include_migration: bool,
) -> Vec<String> {
    let Some(statement) = insert(&Rows {
        header: header(
            database,
            RUN_STATEMENTS_TABLE.0,
            "invocation_id, statement_sequence, step_id, phase, intent, sql, sql_sha256, \
             workflow_sha256, workflow_revision",
        ),
        rows,
    }) else {
        return Vec::new();
    };
    with_migration(database, vec![statement], include_migration)
}

/// Replace each `{name}` placeholder of `template` once, in a single pass, so substituted values
/// are never rescanned.
fn fill(template: &str, values: &[(&str, &str)]) -> String {
    let mut filled = String::with_capacity(template.len());
    let mut rest = template;
    while let Some(start) = rest.find('{') {
        filled.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        let replacement = after.find('}').and_then(|end| {
            values
                .iter()
                .find(|(name, _)| *name == &after[..end])
                .map(|(_, value)| (end, *value))
        });
        match replacement {
            Some((end, value)) => {
                filled.push_str(value);
                rest = &after[end + 1..];
            }
            None => {
                filled.push('{');
                rest = after;
            }
        }
    }
    filled.push_str(rest);
    filled
}

/// Join current manifest fingerprints to the latest terminal result history.
#[must_use]
pub fn latest_node_status_query(
    database: &str,
    project_identity: &str,
    target_identity: &str,
    nodes: &[QualityNode],
) -> String {
    let manifest_sql = if nodes.is_empty() {
        EMPTY_MANIFEST_NODES.to_owned()
    } else {
        let rows: Vec<String> = nodes
            .iter()
            .map(|node| {
                format!(
                    "({}, {}, {}, {}, {}, {}, {})",
                    text_literal(&node.node_kind),
                    text_literal(&node.node_name),
                    text_literal(&node.binding_key),
                    text_literal(&node.definition_fingerprint),
                    text_literal(&node.execution_fingerprint),
                    literal(&node.cadence_seconds.into()),
                    node.warmup_seconds
                )
            })
            .collect();
        format!(
            "SELECT * FROM VALUES('node_kind String, node_name String, binding_key String, \
             definition_fingerprint String, execution_fingerprint String, cadence_seconds \
             Nullable(UInt64), warmup_seconds UInt64', {})",
            rows.join(", ")
        )
    };
    let logical_project = project_identity
        .rsplit_once('/')
        .map_or(project_identity, |(_, name)| name);
    fill(
        LATEST_NODE_STATUS_QUERY,
        &[
            ("manifest_sql", &manifest_sql),
            ("database", database),
            ("target", &text_literal(target_identity)),
            ("project", &text_literal(logical_project)),
            (
                "project_suffix",
                &text_literal(&format!("/{logical_project}")),
            ),
        ],
    )
}
