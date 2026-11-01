//! `CREATE` statements for neutral adapter resources.

use crate::CapabilityError;

const DATABASE_PLACEHOLDER: &str = "__sqltide_target_database__";
const MANAGED_SOURCE_KIND_KAFKA: &str = "kafka";
const REFRESHABLE_VIEW_SETTING: &str = "allow_experimental_refreshable_materialized_view = 1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Column {
    pub name: String,
    pub type_sql: String,
    pub default_expression: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedSource {
    pub source_kind: String,
    pub name: String,
    pub columns: Vec<Column>,
    pub broker_list: String,
    pub topic: String,
    pub consumer_group: String,
    pub format: String,
    pub settings: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Table {
    pub name: String,
    pub columns: Vec<Column>,
    pub engine: String,
    pub order_by: Vec<String>,
    pub partition_by: Option<String>,
    pub ttl: Option<String>,
    pub settings: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializedView {
    pub name: String,
    pub target_relation_name: String,
    pub database_template: String,
    pub refresh: Option<String>,
    pub append: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct View {
    pub name: String,
    pub database_template: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StableView {
    pub name: String,
    pub target_relation_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resource {
    ManagedSource(ManagedSource),
    Table(Table),
    MaterializedView(MaterializedView),
    View(View),
    StableView(StableView),
}

/// One manually executable database creation statement.
#[must_use]
pub fn ensure_database(database: &str) -> String {
    format!("CREATE DATABASE IF NOT EXISTS {database};")
}

/// The Kafka consumer group ClickHouse uses for one target database.
#[must_use]
pub fn database_scoped_consumer_group(consumer_group: &str, database: &str) -> String {
    format!("{consumer_group}_{}", database.replace('-', "_"))
}

/// Render one resource as ClickHouse DDL, with credential placeholders left in place.
///
/// # Errors
///
/// Returns a capability error for managed sources other than Kafka.
pub fn render_resource(
    resource: &Resource,
    database: &str,
    if_not_exists: bool,
) -> Result<String, CapabilityError> {
    Ok(match resource {
        Resource::ManagedSource(source) => {
            if source.source_kind != MANAGED_SOURCE_KIND_KAFKA {
                return Err(CapabilityError(format!(
                    "ClickHouse does not support managed source kind '{}'",
                    source.source_kind
                )));
            }
            managed_source(source, database, if_not_exists)
        }
        Resource::Table(table) => render_table(table, database, if_not_exists),
        Resource::MaterializedView(view) => materialized_view(view, database, if_not_exists),
        Resource::View(view) => format!(
            "CREATE VIEW {}{database}.{} AS\n{}",
            if_not_exists_clause(if_not_exists),
            view.name,
            with_database(&view.database_template, database)
        ),
        Resource::StableView(view) => format!(
            "CREATE OR REPLACE VIEW {database}.{} AS\nSELECT * FROM {database}.{}",
            view.name, view.target_relation_name
        ),
    })
}

fn if_not_exists_clause(if_not_exists: bool) -> &'static str {
    if if_not_exists { "IF NOT EXISTS " } else { "" }
}

fn with_database(template: &str, database: &str) -> String {
    template.replace(&format!("{DATABASE_PLACEHOLDER}."), &format!("{database}."))
}

fn column_definitions(columns: &[Column]) -> String {
    columns
        .iter()
        .map(|column| match &column.default_expression {
            None => format!("{} {}", column.name, column.type_sql),
            Some(default) => format!("{} {} DEFAULT {default}", column.name, column.type_sql),
        })
        .collect::<Vec<_>>()
        .join(",\n    ")
}

fn managed_source(source: &ManagedSource, database: &str, if_not_exists: bool) -> String {
    let consumer_group = database_scoped_consumer_group(&source.consumer_group, database);
    let mut settings: Vec<String> = vec![
        format!("kafka_broker_list = '{}'", source.broker_list),
        format!("kafka_topic_list = '{}'", source.topic),
        format!("kafka_group_name = '{consumer_group}'"),
        format!("kafka_format = '{}'", source.format),
    ];
    settings.extend(
        source
            .settings
            .iter()
            .map(|(name, value)| format!("{name} = '{value}'")),
    );
    format!(
        "CREATE TABLE {}{database}.{} (\n    {}\n) ENGINE = Kafka\nSETTINGS {}",
        if_not_exists_clause(if_not_exists),
        source.name,
        column_definitions(&source.columns),
        settings.join(", ")
    )
}

fn render_table(table: &Table, database: &str, if_not_exists: bool) -> String {
    let mut ddl = format!(
        "CREATE TABLE {}{database}.{} (\n    {}\n) ENGINE = {}\nORDER BY ({})",
        if_not_exists_clause(if_not_exists),
        table.name,
        column_definitions(&table.columns),
        table.engine,
        table.order_by.join(", ")
    );
    if let Some(partition_by) = &table.partition_by {
        ddl.push_str(&format!("\nPARTITION BY {partition_by}"));
    }
    if let Some(ttl) = &table.ttl {
        ddl.push_str(&format!("\nTTL {ttl}"));
    }
    if !table.settings.is_empty() {
        let settings: Vec<String> = table
            .settings
            .iter()
            .map(|(name, value)| format!("{name} = {value}"))
            .collect();
        ddl.push_str(&format!("\nSETTINGS {}", settings.join(", ")));
    }
    ddl
}

fn materialized_view(view: &MaterializedView, database: &str, if_not_exists: bool) -> String {
    let query = with_database(&view.database_template, database);
    let head = format!(
        "CREATE MATERIALIZED VIEW {}{database}.{}\n",
        if_not_exists_clause(if_not_exists),
        view.name
    );
    match &view.refresh {
        None => format!(
            "{head}TO {database}.{} AS\n{query}",
            view.target_relation_name
        ),
        Some(refresh) => format!(
            "{head}REFRESH EVERY {refresh}{}\nTO {database}.{} AS\n{query}\nSETTINGS \
             {REFRESHABLE_VIEW_SETTING}",
            if view.append { " APPEND" } else { "" },
            view.target_relation_name
        ),
    }
}
