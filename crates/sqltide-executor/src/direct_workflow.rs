//! Every statement of a direct build, and the statements that recover its sources, from
//! pre-rendered adapter SQL.

use crate::workflow::{Intent, Phase, Statement, step_segment, terminate_sql, wait_sql};

/// A Kafka table recreated with its declared settings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Recreation {
    pub source_name: String,
    pub database: String,
    pub staged_name: String,
    pub landing_view_name: String,
    /// The staged table's `CREATE` statement.
    pub stage_sql: String,
}

impl Recreation {
    fn drop_staged_sql(&self) -> String {
        terminate_sql(&format!(
            "DROP TABLE IF EXISTS {}.{} SYNC",
            self.database, self.staged_name
        ))
    }
}

/// A replay whose SQL is rendered once its coverage is captured.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeReplay {
    pub model_name: String,
    pub capture_step_id: String,
    pub replay_step_id: String,
    pub coverage_sql: String,
}

/// Everything a direct build's statements are assembled from.
#[derive(Clone, Debug, PartialEq)]
pub struct Input {
    pub database: String,
    pub ensure_database_sql: String,
    /// `(resource name, SQL)` of absent source resources.
    pub source_realizations: Vec<(String, String)>,
    pub recreations: Vec<Recreation>,
    /// Names of preserved landing views, in order.
    pub preserved_landing_view_names: Vec<String>,
    /// `(name, SQL)` of landing views created for absent sources.
    pub bootstrapped_landing_views: Vec<(String, String)>,
    /// `(name, SQL)` of every paused landing view to activate after replay: the preserved
    /// ones, then the bootstrapped ones.
    pub activations: Vec<(String, String)>,
    pub stabilization_seconds: f64,
    /// `(relation name, is a view)` of relations to drop.
    pub teardown: Vec<(String, bool)>,
    /// `(resource name, SQL)` of objects to realize.
    pub realizations: Vec<(String, String)>,
    pub runtime_replays: Vec<RuntimeReplay>,
    /// `(name, query)` of audits.
    pub audits: Vec<(String, String)>,
    /// How many failing rows each audit samples.
    pub audit_sample_limit: usize,
}

/// The workflow statements and the source recovery statements.
#[must_use]
pub fn statements(input: &Input) -> (Vec<Statement>, Vec<Statement>) {
    let mut statements: Vec<Statement> = preparation(input);
    for (relation_name, is_view) in &input.teardown {
        let relation_type: &str = if *is_view { "VIEW" } else { "TABLE" };
        statements.push(Statement::mutation(
            format!("drop_{}", step_segment(relation_name)),
            Phase::Teardown,
            &format!(
                "DROP {relation_type} IF EXISTS {}.{relation_name} SYNC;",
                input.database
            ),
        ));
    }
    for (name, sql) in &input.realizations {
        statements.push(Statement::mutation(
            format!("realize_{}", step_segment(name)),
            Phase::Realization,
            &terminate_sql(sql),
        ));
    }
    statements.push(Statement::new(
        "wait_for_live_stabilization",
        Phase::Stabilization,
        Intent::Wait,
        wait_sql(input.stabilization_seconds),
    ));
    for replay in &input.runtime_replays {
        statements.push(Statement::new(
            replay.capture_step_id.clone(),
            Phase::Replay,
            Intent::Query,
            terminate_sql(&replay.coverage_sql),
        ));
        statements.push(Statement::new(
            replay.replay_step_id.clone(),
            Phase::Replay,
            Intent::Mutation,
            format!("/* runtime replay capture: {} */;", replay.model_name),
        ));
    }
    for (name, sql) in &input.activations {
        statements.push(Statement::mutation(
            activation_step(name),
            Phase::Replay,
            sql,
        ));
    }
    for (position, (name, query)) in input.audits.iter().enumerate() {
        let prefix: String = format!("audit_{}_{}", position + 1, step_segment(name));
        for (suffix, sql) in [
            (
                "count",
                format!(
                    "SELECT count() AS failing_row_count FROM (\n{query}\n) AS __sqltide_audit;"
                ),
            ),
            (
                "sample",
                format!(
                    "SELECT * FROM (\n{query}\n) AS __sqltide_audit LIMIT {};",
                    input.audit_sample_limit
                ),
            ),
        ] {
            let mut statement: Statement = Statement::new(
                format!("{prefix}_{suffix}"),
                Phase::Audit,
                Intent::Query,
                sql,
            );
            statement.continue_on_error = true;
            statements.push(statement);
        }
    }
    let recovery: Vec<Statement> = recovery(&statements, input);
    (statements, recovery)
}

fn activation_step(landing_view_name: &str) -> String {
    format!("activate_source_{}", step_segment(landing_view_name))
}

fn pause(input: &Input, landing_view_name: &str) -> Statement {
    Statement::mutation(
        format!("pause_source_{}", step_segment(landing_view_name)),
        Phase::Preparation,
        &format!(
            "DROP TABLE IF EXISTS {}.{landing_view_name} SYNC",
            input.database
        ),
    )
}

fn preparation(input: &Input) -> Vec<Statement> {
    let mut statements: Vec<Statement> = vec![Statement::mutation(
        "prepare_target_database",
        Phase::Preparation,
        &input.ensure_database_sql,
    )];
    for (name, sql) in &input.source_realizations {
        statements.push(Statement::mutation(
            format!("prepare_source_{}", step_segment(name)),
            Phase::Preparation,
            &terminate_sql(sql),
        ));
    }
    let early_paused: Vec<&str> = input
        .recreations
        .iter()
        .map(|recreation| recreation.landing_view_name.as_str())
        .filter(|name| {
            input
                .preserved_landing_view_names
                .iter()
                .any(|preserved| preserved == name)
        })
        .collect();
    for recreation in &input.recreations {
        if early_paused.contains(&recreation.landing_view_name.as_str()) {
            statements.push(pause(input, &recreation.landing_view_name));
        }
        let segment: String = step_segment(&recreation.source_name);
        let drop_staged: String = recreation.drop_staged_sql();
        statements.push(Statement::mutation(
            format!("clear_staged_source_{segment}"),
            Phase::Preparation,
            &drop_staged,
        ));
        statements.push(Statement::mutation(
            format!("stage_source_{segment}"),
            Phase::Preparation,
            &recreation.stage_sql,
        ));
        statements.push(Statement::mutation(
            format!("swap_source_{segment}"),
            Phase::Preparation,
            &format!(
                "EXCHANGE TABLES {database}.{} AND {database}.{}",
                recreation.source_name,
                recreation.staged_name,
                database = recreation.database
            ),
        ));
        statements.push(Statement::mutation(
            format!("retire_source_{segment}"),
            Phase::Preparation,
            &drop_staged,
        ));
    }
    for (name, sql) in &input.bootstrapped_landing_views {
        statements.push(Statement::mutation(
            format!("bootstrap_source_{}", step_segment(name)),
            Phase::Preparation,
            sql,
        ));
    }
    if !input.bootstrapped_landing_views.is_empty() && input.stabilization_seconds > 0.0 {
        statements.push(Statement::new(
            "wait_for_source_bootstrap",
            Phase::Preparation,
            Intent::Wait,
            wait_sql(input.stabilization_seconds),
        ));
    }
    for name in input.preserved_landing_view_names.iter().chain(
        input
            .bootstrapped_landing_views
            .iter()
            .map(|(name, _)| name),
    ) {
        if !early_paused.contains(&name.as_str()) {
            statements.push(pause(input, name));
        }
    }
    statements
}

/// Drop each staged Kafka table, then recreate each paused landing view as it was activated.
fn recovery(statements: &[Statement], input: &Input) -> Vec<Statement> {
    let mut recovery: Vec<Statement> = input
        .recreations
        .iter()
        .map(|recreation| {
            Statement::new(
                format!(
                    "recover_staged_source_{}",
                    step_segment(&recreation.source_name)
                ),
                Phase::Finalization,
                Intent::Mutation,
                recreation.drop_staged_sql(),
            )
        })
        .collect();
    for (name, _) in &input.activations {
        let step_id: String = activation_step(name);
        let Some(activation) = statements
            .iter()
            .rev()
            .find(|statement| statement.step_id == step_id)
        else {
            continue;
        };
        let mut statement: Statement = activation.clone();
        statement.step_id = format!("recover_source_{}", step_segment(name));
        statement.phase = Phase::Finalization;
        recovery.push(statement);
    }
    recovery
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input() -> Input {
        Input {
            database: "analytics".to_owned(),
            ensure_database_sql: "CREATE DATABASE IF NOT EXISTS analytics".to_owned(),
            source_realizations: vec![("orders_landing".to_owned(), "CREATE TABLE l".to_owned())],
            recreations: vec![Recreation {
                source_name: "orders_kafka".to_owned(),
                database: "analytics".to_owned(),
                staged_name: "orders_kafka__staged".to_owned(),
                landing_view_name: "orders_mv".to_owned(),
                stage_sql: "CREATE TABLE staged".to_owned(),
            }],
            preserved_landing_view_names: vec!["orders_mv".to_owned(), "items_mv".to_owned()],
            bootstrapped_landing_views: vec![(
                "events_mv".to_owned(),
                "CREATE MV events".to_owned(),
            )],
            activations: vec![
                ("orders_mv".to_owned(), "CREATE MV orders".to_owned()),
                ("items_mv".to_owned(), "CREATE MV items".to_owned()),
                ("events_mv".to_owned(), "CREATE MV events".to_owned()),
            ],
            stabilization_seconds: 2.0,
            teardown: vec![("summary".to_owned(), true), ("orders".to_owned(), false)],
            realizations: vec![("orders".to_owned(), "CREATE TABLE orders;;".to_owned())],
            runtime_replays: vec![RuntimeReplay {
                model_name: "orders".to_owned(),
                capture_step_id: "capture_replay_orders".to_owned(),
                replay_step_id: "replay_orders".to_owned(),
                coverage_sql: "SELECT coverage".to_owned(),
            }],
            audits: vec![("not null".to_owned(), "SELECT 1".to_owned())],
            audit_sample_limit: 5,
        }
    }

    #[test]
    fn pauses_recreated_sources_first_and_reactivates_every_landing_view() {
        let (statements, recovery) = statements(&input());
        let steps: Vec<(&str, &str)> = statements
            .iter()
            .map(|statement| (statement.step_id.as_str(), statement.phase.as_str()))
            .collect();
        assert_eq!(
            steps,
            vec![
                ("prepare_target_database", "preparation"),
                ("prepare_source_orders_landing", "preparation"),
                ("pause_source_orders_mv", "preparation"),
                ("clear_staged_source_orders_kafka", "preparation"),
                ("stage_source_orders_kafka", "preparation"),
                ("swap_source_orders_kafka", "preparation"),
                ("retire_source_orders_kafka", "preparation"),
                ("bootstrap_source_events_mv", "preparation"),
                ("wait_for_source_bootstrap", "preparation"),
                ("pause_source_items_mv", "preparation"),
                ("pause_source_events_mv", "preparation"),
                ("drop_summary", "teardown"),
                ("drop_orders", "teardown"),
                ("realize_orders", "realization"),
                ("wait_for_live_stabilization", "stabilization"),
                ("capture_replay_orders", "replay"),
                ("replay_orders", "replay"),
                ("activate_source_orders_mv", "replay"),
                ("activate_source_items_mv", "replay"),
                ("activate_source_events_mv", "replay"),
                ("audit_1_not_null_count", "audit"),
                ("audit_1_not_null_sample", "audit"),
            ]
        );
        assert_eq!(
            statements[5].sql,
            "EXCHANGE TABLES analytics.orders_kafka AND analytics.orders_kafka__staged;"
        );
        assert_eq!(
            statements[11].sql,
            "DROP VIEW IF EXISTS analytics.summary SYNC;"
        );
        assert_eq!(statements[13].sql, "CREATE TABLE orders;");
        assert_eq!(statements[16].sql, "/* runtime replay capture: orders */;");
        assert_eq!(
            statements[21].sql,
            "SELECT * FROM (\nSELECT 1\n) AS __sqltide_audit LIMIT 5;"
        );
        assert!(statements[21].continue_on_error);
        let recovery_steps: Vec<(&str, &str)> = recovery
            .iter()
            .map(|statement| (statement.step_id.as_str(), statement.sql.as_str()))
            .collect();
        assert_eq!(
            recovery_steps,
            vec![
                (
                    "recover_staged_source_orders_kafka",
                    "DROP TABLE IF EXISTS analytics.orders_kafka__staged SYNC;"
                ),
                ("recover_source_orders_mv", "CREATE MV orders;"),
                ("recover_source_items_mv", "CREATE MV items;"),
                ("recover_source_events_mv", "CREATE MV events;"),
            ]
        );
    }

    #[test]
    fn skips_the_bootstrap_wait_without_stabilization() {
        let mut quick: Input = input();
        quick.stabilization_seconds = 0.0;
        let (statements, _) = statements(&quick);
        assert!(
            !statements
                .iter()
                .any(|statement| statement.step_id == "wait_for_source_bootstrap")
        );
    }
}
