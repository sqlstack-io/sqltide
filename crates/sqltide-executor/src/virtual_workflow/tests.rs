use super::*;

fn replay(mode: Mode) -> Replay {
    Replay {
        mode,
        database: "analytics".to_owned(),
        root: "orders".to_owned(),
        anchor: "raw__orders".to_owned(),
        target: "orders__dep".to_owned(),
        partition_column: "_replay_partition".to_owned(),
        offset_column: "_replay_offset".to_owned(),
        timestamp_column: "_replay_timestamp".to_owned(),
        landed_at_column: String::new(),
        cursor_column: "_replay_cursor".to_owned(),
        forced_lower_bound: false,
        forced_start_time: None,
    }
}

fn root(name: &str, mode: Mode, rendered: usize) -> Root {
    Root {
        name: name.to_owned(),
        replay: replay(mode),
        persist_watermarks: true,
        boundary_key: (None, "table".to_owned(), "raw__orders".to_owned()),
        anchor_existed_at_confirmation: true,
        rendered: (0..rendered)
            .map(|index| format!("INSERT {index}"))
            .collect(),
    }
}

fn input(roots: Vec<Root>) -> Input {
    Input {
        metadata_database: "meta".to_owned(),
        default_database: "analytics".to_owned(),
        deployment_id: "dep".to_owned(),
        deployment: Deployment {
            deployment_id: "dep".to_owned(),
            created_at: "2026-01-01 00:00:00.000".to_owned(),
            replay_lineage_mode: "offsets".to_owned(),
            workflow_fingerprint: "abc".to_owned(),
            tool_version: "0.54.0".to_owned(),
        },
        boundary_time: None,
        deployments_table_exists: false,
        publish_table_exists: true,
        prepared: vec![
            ("orders__dep".to_owned(), None),
            (
                "summary__dep".to_owned(),
                Some("CREATE VIEW x AS SELECT 'a'".to_owned()),
            ),
        ],
        ensure_database_sql: "CREATE DATABASE IF NOT EXISTS analytics;".to_owned(),
        migrate_metadata_sql: vec!["CREATE TABLE m1".to_owned(), "CREATE TABLE m2;".to_owned()],
        source_realizations: vec![("raw.orders".to_owned(), "CREATE TABLE raw".to_owned())],
        persist_metadata_sql: vec!["INSERT m".to_owned()],
        realizations: vec![("orders__dep".to_owned(), "CREATE TABLE o".to_owned())],
        landing_views: vec![(
            "orders_mv".to_owned(),
            "CREATE MATERIALIZED VIEW mv".to_owned(),
        )],
        stabilization_seconds: 1.5,
        roots,
    }
}

#[test]
fn orders_every_phase_and_names_each_step() {
    let statements = statements(&input(vec![
        root("orders", Mode::Offsets, 2),
        root("items", Mode::Timestamp, 1),
    ]));
    let steps: Vec<(&str, &str, &str)> = statements
        .iter()
        .map(|statement| {
            (
                statement.step_id.as_str(),
                statement.phase.as_str(),
                statement.intent.as_str(),
            )
        })
        .collect();
    assert_eq!(
        steps,
        vec![
            ("assert_candidate_metadata", "preflight", "assertion"),
            ("assert_candidate_unpublished", "preflight", "assertion"),
            (
                "assert_candidate_relation_orders__dep",
                "preflight",
                "assertion"
            ),
            (
                "assert_candidate_relation_summary__dep",
                "preflight",
                "assertion"
            ),
            ("prepare_target_database", "preparation", "mutation"),
            ("prepare_metadata_1", "preparation", "mutation"),
            ("prepare_metadata_2", "preparation", "mutation"),
            ("prepare_source_raw_orders", "preparation", "mutation"),
            ("persist_candidate_metadata_1", "preparation", "mutation"),
            ("realize_orders__dep", "realization", "mutation"),
            ("attach_source_orders_mv", "realization", "mutation"),
            (
                "wait_for_virtual_live_stabilization",
                "stabilization",
                "wait"
            ),
            ("capture_boundary_time", "boundary", "mutation"),
            ("capture_watermark_orders", "replay", "mutation"),
            ("assert_qualifying_input_orders", "replay", "assertion"),
            ("seed_orders", "replay", "mutation"),
            ("replay_orders", "replay", "mutation"),
            ("capture_watermark_items", "replay", "mutation"),
            ("assert_qualifying_input_items", "replay", "assertion"),
            ("replay_items", "replay", "mutation"),
            ("read_readiness_orders", "audit", "query"),
            ("assert_readiness_orders", "audit", "assertion"),
            ("read_readiness_items", "audit", "query"),
            ("assert_readiness_items", "audit", "assertion"),
            ("read_boundary_time", "audit", "query"),
        ]
    );
    assert_eq!(statements[6].sql, "CREATE TABLE m2;");
    assert_eq!(
        statements[3].sql,
        "SELECT throwIf(count() != 1 OR any(create_table_query) != 'CREATE VIEW x AS SELECT \
         \\'a\\'', 'Candidate relation changed after virtual confirmation') FROM system.tables \
         WHERE database = 'analytics' AND name = 'summary__dep';"
    );
}

#[test]
fn captures_offset_watermarks_up_to_the_boundary_time() {
    let statements = statements(&input(vec![root("orders", Mode::Offsets, 1)]));
    assert_eq!(
        statements[13].sql,
        "INSERT INTO meta._sqltide_virtual_replay_boundaries (deployment_id, root_database_name, \
         root_object_type, root_object_name, anchor_database_name, anchor_object_type, \
         anchor_object_name, boundary_kind, value_kind, partition_value, lower_value, \
         cutoff_value, cutoff_inclusive, captured_at)\nSELECT 'dep', 'analytics', 'table', \
         'orders', 'analytics', 'table', 'raw__orders', 'offsets', 'integer', \
         toString(_replay_partition), NULL, toString(max(_replay_offset)), true, now64(3, \
         'UTC')\nFROM analytics.raw__orders\nWHERE _replay_timestamp <= (SELECT \
         any(boundary_time) FROM meta._sqltide_virtual_deployments WHERE deployment_id = \
         'dep')\nGROUP BY _replay_partition\nHAVING NOT EXISTS (SELECT 1 FROM \
         meta._sqltide_virtual_replay_boundaries WHERE deployment_id = 'dep' AND \
         root_object_name = 'orders' AND boundary_kind = 'offsets');"
    );
}

#[test]
fn bounds_forced_scalar_qualifying_input_from_the_start_time() {
    let mut forced: Root = root("orders", Mode::LandedAt, 1);
    forced.replay.landed_at_column = "_replay_landed_at".to_owned();
    forced.replay.forced_lower_bound = true;
    forced.replay.forced_start_time = Some("2026-01-01 00:00:00".to_owned());
    let mut unset: Root = root("items", Mode::Cursor, 1);
    unset.replay.forced_lower_bound = true;
    let mut absent: Root = root("events", Mode::Cursor, 1);
    absent.anchor_existed_at_confirmation = false;
    let statements = statements(&input(vec![forced, unset, absent]));
    assert!(statements[14].sql.ends_with(
        "AND source._replay_landed_at <= CAST(watermark.cutoff_value AS DateTime64(3, 'UTC')) \
         AND source._replay_landed_at >= toDateTime64('2026-01-01 00:00:00', 3, 'UTC')) AS \
         qualifying_rows);"
    ));
    assert!(
        statements[17]
            .sql
            .ends_with("CAST(watermark.cutoff_value AS UInt64) AND false) AS qualifying_rows);")
    );
    assert_eq!(statements[20].sql, "SELECT 0;");
}
