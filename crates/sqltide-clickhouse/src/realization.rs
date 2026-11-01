//! How logical sources become ClickHouse relations: the names, landing schema, and landing
//! query of a Kafka source, and the relation SQL adopted sources are read through.

const KAFKA_TABLE_PREFIX: &str = "kafka__";
const RAW_TABLE_PREFIX: &str = "raw__";
const MATERIALIZED_VIEW_PREFIX: &str = "mv__";
const MANAGED_SOURCE_KIND_KAFKA: &str = "kafka";
const SUPPORTED_KAFKA_FORMAT: &str = "JSONAsString";
/// Kafka settings the framework derives from typed source fields.
const FRAMEWORK_KAFKA_SETTING_KEYS: [&str; 4] = [
    "kafka_broker_list",
    "kafka_topic_list",
    "kafka_group_name",
    "kafka_format",
];
const SECRET_PLACEHOLDER_PREFIX: &str = "__sqltide_secret__";
const SECRET_PLACEHOLDER_SUFFIX: &str = "__";
/// The replay lineage columns adopted sources are aliased onto, in projection order.
const REPLAY_COLUMNS: [&str; 5] = [
    "_replay_partition",
    "_replay_offset",
    "_replay_timestamp",
    "_replay_landed_at",
    "_replay_cursor",
];

/// The raw landing table's `(name, type)` columns.
pub const RAW_LANDING_COLUMNS: [(&str, &str); 13] = [
    ("kafka_key", "String"),
    ("kafka_value", "String"),
    ("kafka_topic", "String"),
    ("kafka_partition", "Int32"),
    ("kafka_offset", "Int64"),
    ("kafka_timestamp", "Nullable(DateTime64(3))"),
    ("_replay_partition", "Int32"),
    ("_replay_offset", "Int64"),
    ("_replay_timestamp", "Nullable(DateTime64(3))"),
    ("kafka_header_keys", "Array(String)"),
    ("kafka_header_values", "Array(String)"),
    ("kafka_landed_at", "DateTime64(3)"),
    ("_replay_landed_at", "DateTime64(3)"),
];

/// The fields of a managed source request that decide its realization.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ManagedSourceRequest<'a> {
    pub logical_name: &'a str,
    pub source_kind: &'a str,
    pub format: &'a str,
    pub consumer_group: Option<&'a str>,
    pub project_name: Option<&'a str>,
    pub target_name: Option<&'a str>,
    pub setting_keys: Vec<&'a str>,
}

/// The relations and landing query one Kafka source realizes as.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ManagedSourceRealization {
    pub kafka_table_name: String,
    pub raw_table_name: String,
    pub materialized_view_name: String,
    pub consumer_group: String,
    pub landing_query: String,
}

/// Realize one managed source, or the configuration error that prevents it.
///
/// # Errors
/// A message when the source kind, format, or settings cannot be realized in ClickHouse.
pub fn realize_managed_source(
    request: &ManagedSourceRequest<'_>,
) -> Result<ManagedSourceRealization, String> {
    if request.source_kind != MANAGED_SOURCE_KIND_KAFKA {
        return Err(format!(
            "ClickHouse cannot realize managed source kind '{}'",
            request.source_kind
        ));
    }
    if request.format != SUPPORTED_KAFKA_FORMAT {
        return Err(format!(
            "ClickHouse Kafka landing currently supports only the '{SUPPORTED_KAFKA_FORMAT}' \
             format; got '{}'",
            request.format
        ));
    }
    let mut overlapping: Vec<&str> = request
        .setting_keys
        .iter()
        .copied()
        .filter(|key| FRAMEWORK_KAFKA_SETTING_KEYS.contains(key))
        .collect();
    if !overlapping.is_empty() {
        overlapping.sort_unstable();
        return Err(format!(
            "Kafka settings override map cannot redefine typed Kafka settings: {}",
            overlapping.join(", ")
        ));
    }
    let kafka_table_name: String = format!("{KAFKA_TABLE_PREFIX}{}", request.logical_name);
    let consumer_group: String = match request.consumer_group {
        Some(group) if !group.is_empty() => group.to_owned(),
        // The pre-rename group, so existing sources resume from their committed offsets.
        _ => {
            let segments: Vec<&str> = [
                request.project_name,
                request.target_name,
                Some(request.logical_name),
            ]
            .into_iter()
            .flatten()
            .collect();
            format!("streambuild_{}", segments.join("_"))
        }
    };
    Ok(ManagedSourceRealization {
        raw_table_name: format!("{RAW_TABLE_PREFIX}{}", request.logical_name),
        materialized_view_name: format!("{MATERIALIZED_VIEW_PREFIX}{}", request.logical_name),
        consumer_group,
        landing_query: landing_query(&kafka_table_name),
        kafka_table_name,
    })
}

fn landing_query(kafka_table_name: &str) -> String {
    format!(
        "SELECT\n    _key AS kafka_key,\n    message AS kafka_value,\n    _topic AS kafka_topic,\n    \
         _partition AS kafka_partition,\n    _offset AS kafka_offset,\n    \
         _timestamp_ms AS kafka_timestamp,\n    _partition AS _replay_partition,\n    \
         _offset AS _replay_offset,\n    _timestamp_ms AS _replay_timestamp,\n    \
         _headers.name AS kafka_header_keys,\n    _headers.value AS kafka_header_values,\n    \
         now64(3) AS kafka_landed_at,\n    now64(3) AS _replay_landed_at\nFROM {kafka_table_name}"
    )
}

/// The relation an adopted table is read through: the table itself, or a projection that
/// aliases its authored `[partition, offset, timestamp, landed at, cursor]` lineage columns
/// onto the replay column names.
#[must_use]
pub fn external_source_relation_sql(table_name: &str, columns: [Option<&str>; 5]) -> String {
    let aliases: Vec<String> = columns
        .iter()
        .zip(REPLAY_COLUMNS)
        .filter_map(|(column, replay_column)| {
            column
                .filter(|column| *column != replay_column)
                .map(|column| format!("{column} AS {replay_column}"))
        })
        .collect();
    if aliases.is_empty() {
        return table_name.to_owned();
    }
    format!(
        "(SELECT\n    *,\n    {}\nFROM {table_name})",
        aliases.join(",\n    ")
    )
}

/// The `postgresql()` table function a refresh source reads, with the password as a secret
/// placeholder so no credential reaches an artifact.
#[must_use]
pub fn postgres_source_relation_sql(
    host: &str,
    port: &str,
    database: &str,
    table: &str,
    user: &str,
    password_env: Option<&str>,
) -> String {
    let password: String = password_env.map_or_else(String::new, |name| {
        format!("{SECRET_PLACEHOLDER_PREFIX}{name}{SECRET_PLACEHOLDER_SUFFIX}")
    });
    format!("postgresql('{host}:{port}', '{database}', '{table}', '{user}', '{password}')")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(consumer_group: Option<&str>) -> ManagedSourceRequest<'_> {
        ManagedSourceRequest {
            logical_name: "orders",
            source_kind: "kafka",
            format: "JSONAsString",
            consumer_group,
            project_name: Some("shop"),
            target_name: None,
            setting_keys: vec!["kafka_num_consumers"],
        }
    }

    #[test]
    fn realizes_kafka_sources_with_prefixed_names_and_a_default_group() {
        let realization = realize_managed_source(&request(Some(""))).unwrap();
        assert_eq!(realization.kafka_table_name, "kafka__orders");
        assert_eq!(realization.raw_table_name, "raw__orders");
        assert_eq!(realization.materialized_view_name, "mv__orders");
        assert_eq!(realization.consumer_group, "streambuild_shop_orders");
        assert!(realization.landing_query.ends_with("\nFROM kafka__orders"));
        assert_eq!(
            realize_managed_source(&request(Some("group")))
                .unwrap()
                .consumer_group,
            "group"
        );
    }

    #[test]
    fn rejects_unsupported_kinds_formats_and_typed_setting_overrides() {
        let mut bad = request(None);
        bad.setting_keys = vec!["kafka_topic_list", "kafka_format"];
        assert_eq!(
            realize_managed_source(&bad),
            Err(
                "Kafka settings override map cannot redefine typed Kafka settings: \
                 kafka_format, kafka_topic_list"
                    .to_owned()
            )
        );
        bad.format = "CSV";
        assert!(
            realize_managed_source(&bad)
                .unwrap_err()
                .ends_with("got 'CSV'")
        );
        bad.source_kind = "pulsar";
        assert_eq!(
            realize_managed_source(&bad),
            Err("ClickHouse cannot realize managed source kind 'pulsar'".to_owned())
        );
    }

    #[test]
    fn aliases_only_authored_lineage_columns_with_other_names() {
        assert_eq!(
            external_source_relation_sql(
                "orders",
                [None, Some("_replay_offset"), None, None, None]
            ),
            "orders"
        );
        assert_eq!(
            external_source_relation_sql("orders", [Some("p"), None, Some("ts"), None, Some("id")]),
            "(SELECT\n    *,\n    p AS _replay_partition,\n    ts AS _replay_timestamp,\n    \
             id AS _replay_cursor\nFROM orders)"
        );
    }

    #[test]
    fn renders_postgres_sources_with_a_password_placeholder() {
        assert_eq!(
            postgres_source_relation_sql("h", "5432", "db", "t", "u", Some("PG_PASSWORD")),
            "postgresql('h:5432', 'db', 't', 'u', '__sqltide_secret__PG_PASSWORD__')"
        );
        assert_eq!(
            postgres_source_relation_sql("h", "1", "db", "t", "u", None),
            "postgresql('h:1', 'db', 't', 'u', '')"
        );
    }
}
