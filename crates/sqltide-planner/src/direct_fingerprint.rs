//! The semantic identity a direct build records for each applied model, and how a compiled
//! model has drifted from its recorded baseline.

use serde_json::Value;
use sqltide_json::{JsonDialect, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions, dumps};

use crate::direct::sha256_hex;

/// The `MODEL` header storage that participates in a table model's identity.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Storage {
    pub engine: String,
    pub order_by: Vec<String>,
    pub partition_by: Option<String>,
    pub ttl: Option<String>,
    pub settings: Option<Vec<(String, String)>>,
}

/// The allowlisted semantic identity of one compiled model.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelIdentity {
    pub model_name: String,
    pub kind: String,
    pub relation_name: String,
    /// `(name, type)` of each output column.
    pub output_columns: Vec<(String, String)>,
    /// Present exactly for table models.
    pub storage: Option<Storage>,
}

/// The fields of one direct fingerprint record that identity determines.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Record {
    pub fingerprint_id: String,
    pub logical_model_identity: String,
    pub definition_hash: String,
    pub identity_metadata: String,
}

/// The record a direct build writes for `identity` defined by `query`.
#[must_use]
pub fn record(identity: &ModelIdentity, query: &str, database: &str, workflow_id: &str) -> Record {
    let definition_hash: String = sha256_hex(query);
    let logical_model_identity: String = format!("{database}.{}", identity.model_name);
    Record {
        fingerprint_id: sha256_hex(&format!(
            "{logical_model_identity}:{definition_hash}:{workflow_id}"
        )),
        logical_model_identity,
        definition_hash,
        identity_metadata: identity_metadata(identity),
    }
}

/// `identity` as sorted, compact, ASCII-only JSON.
#[must_use]
pub fn identity_metadata(identity: &ModelIdentity) -> String {
    let mut options = StdlibJsonOptions::new(None).with_separators(",", ":");
    options.sort_keys = true;
    dumps(&json_identity(identity), &JsonDialect::Stdlib(options))
        .expect("model identities hold only strings, integers, and nulls")
}

/// How the model differs from its `(definition hash, identity metadata)` baseline, in the
/// order `missing`, or `query`, `storage`, `identity`.
#[must_use]
pub fn drift_reasons(
    identity: &ModelIdentity,
    query: &str,
    baseline: Option<(&str, &str)>,
) -> Vec<&'static str> {
    let Some((definition_hash, metadata)) = baseline else {
        return vec!["missing"];
    };
    let mut reasons: Vec<&'static str> = Vec::new();
    if sha256_hex(query) != definition_hash {
        reasons.push("query");
    }
    // Metadata that is not a JSON object compares as no identity at all.
    let recorded: Option<serde_json::Map<String, Value>> = match serde_json::from_str(metadata) {
        Ok(Value::Object(map)) => Some(map),
        _ => None,
    };
    let Value::Object(desired) = serde_identity(identity) else {
        unreachable!("identities are objects");
    };
    if identity.storage.is_some() {
        let recorded_storage: &Value = recorded
            .as_ref()
            .and_then(|map| map.get("storage"))
            .unwrap_or(&Value::Null);
        if !python_eq(recorded_storage, &desired["storage"]) {
            reasons.push("storage");
        }
    }
    let identity_matches: bool = recorded.as_ref().is_some_and(|recorded| {
        non_storage_len(recorded) == non_storage_len(&desired)
            && desired
                .iter()
                .filter(|(key, _)| *key != "storage")
                .all(|(key, value)| {
                    recorded
                        .get(key)
                        .is_some_and(|other| python_eq(other, value))
                })
    });
    if !identity_matches {
        reasons.push("identity");
    }
    reasons
}

fn non_storage_len(map: &serde_json::Map<String, Value>) -> usize {
    map.keys().filter(|key| *key != "storage").count()
}

/// Whether Python's `==` holds between the values `json.loads` would build, where booleans
/// and integers compare as numbers.
fn python_eq(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(l, r)| python_eq(l, r))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, value)| right.get(key).is_some_and(|other| python_eq(value, other)))
        }
        _ => match (number(left), number(right)) {
            (Some(left), Some(right)) => left == right,
            _ => false,
        },
    }
}

/// A boolean or number as `f64`, exact for every integer a model identity holds.
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(flag) => Some(f64::from(u8::from(*flag))),
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

fn text(value: &str) -> JsonValue {
    JsonValue::String(value.to_owned())
}

fn optional_text(value: Option<&String>) -> JsonValue {
    value.map_or(JsonValue::Null, |value| text(value))
}

fn object(fields: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        fields
            .into_iter()
            .map(|(key, value)| (JsonKey::Text(key.to_owned()), value))
            .collect(),
    )
}

fn json_identity(identity: &ModelIdentity) -> JsonValue {
    let storage: JsonValue = identity
        .storage
        .as_ref()
        .map_or(JsonValue::Null, |storage| {
            object(vec![
                ("engine", text(&storage.engine)),
                (
                    "order_by",
                    JsonValue::Array(storage.order_by.iter().map(|value| text(value)).collect()),
                ),
                ("partition_by", optional_text(storage.partition_by.as_ref())),
                ("ttl", optional_text(storage.ttl.as_ref())),
                (
                    "settings",
                    storage
                        .settings
                        .as_ref()
                        .map_or(JsonValue::Null, |settings| {
                            JsonValue::Object(
                                settings
                                    .iter()
                                    .map(|(key, value)| (JsonKey::Text(key.clone()), text(value)))
                                    .collect(),
                            )
                        }),
                ),
            ])
        });
    object(vec![
        (
            "identity_version",
            JsonValue::Integer(JsonInteger::from(1_i64)),
        ),
        ("model_name", text(&identity.model_name)),
        ("kind", text(&identity.kind)),
        ("relation_name", text(&identity.relation_name)),
        (
            "output_columns",
            JsonValue::Array(
                identity
                    .output_columns
                    .iter()
                    .map(|(name, column_type)| {
                        object(vec![("name", text(name)), ("type", text(column_type))])
                    })
                    .collect(),
            ),
        ),
        ("storage", storage),
    ])
}

fn serde_identity(identity: &ModelIdentity) -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    let optional = |value: Option<&String>| value.map_or(Value::Null, |value| text(value));
    let storage: Value = identity.storage.as_ref().map_or(Value::Null, |storage| {
        serde_json::json!({
            "engine": storage.engine,
            "order_by": storage.order_by,
            "partition_by": optional(storage.partition_by.as_ref()),
            "ttl": optional(storage.ttl.as_ref()),
            "settings": storage.settings.as_ref().map_or(Value::Null, |settings| {
                Value::Object(
                    settings
                        .iter()
                        .map(|(key, value)| (key.clone(), text(value)))
                        .collect(),
                )
            }),
        })
    });
    serde_json::json!({
        "identity_version": 1,
        "model_name": identity.model_name,
        "kind": identity.kind,
        "relation_name": identity.relation_name,
        "output_columns": identity
            .output_columns
            .iter()
            .map(|(name, column_type)| serde_json::json!({"name": name, "type": column_type}))
            .collect::<Vec<Value>>(),
        "storage": storage,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table_identity() -> ModelIdentity {
        ModelIdentity {
            model_name: "orders.daily".to_owned(),
            kind: "table".to_owned(),
            relation_name: "tbl__daily".to_owned(),
            output_columns: vec![("id".to_owned(), "UInt64".to_owned())],
            storage: Some(Storage {
                engine: "MergeTree".to_owned(),
                order_by: vec!["id".to_owned()],
                partition_by: None,
                ttl: None,
                settings: Some(vec![
                    ("z".to_owned(), "1".to_owned()),
                    ("a".to_owned(), "é".to_owned()),
                ]),
            }),
        }
    }

    #[test]
    fn metadata_is_sorted_compact_ascii_json() {
        assert_eq!(
            identity_metadata(&table_identity()),
            "{\"identity_version\":1,\"kind\":\"table\",\"model_name\":\"orders.daily\",\
             \"output_columns\":[{\"name\":\"id\",\"type\":\"UInt64\"}],\
             \"relation_name\":\"tbl__daily\",\"storage\":{\"engine\":\"MergeTree\",\
             \"order_by\":[\"id\"],\"partition_by\":null,\"settings\":{\"a\":\"\\u00e9\",\
             \"z\":\"1\"},\"ttl\":null}}"
        );
    }

    #[test]
    fn records_hash_the_query_and_identity() {
        let record = record(&table_identity(), "SELECT 1", "db", "w1");
        assert_eq!(record.logical_model_identity, "db.orders.daily");
        assert_eq!(record.definition_hash, sha256_hex("SELECT 1"));
        assert_eq!(
            record.fingerprint_id,
            sha256_hex(&format!("db.orders.daily:{}:w1", sha256_hex("SELECT 1")))
        );
    }

    #[test]
    fn drift_reasons_compare_query_storage_and_identity() {
        let identity = table_identity();
        let metadata = identity_metadata(&identity);
        let hash = sha256_hex("SELECT 1");
        assert_eq!(drift_reasons(&identity, "SELECT 1", None), ["missing"]);
        assert!(drift_reasons(&identity, "SELECT 1", Some((&hash, &metadata))).is_empty());
        assert_eq!(
            drift_reasons(&identity, "SELECT 2", Some((&hash, &metadata))),
            ["query"]
        );
        // Python compares `true` equal to `1`, and key order never matters.
        let reordered = metadata
            .replace("\"identity_version\":1", "\"identity_version\":true")
            .replace("\"kind\":\"table\",", "")
            .replacen('{', "{\"kind\":\"table\",", 1);
        assert!(drift_reasons(&identity, "SELECT 1", Some((&hash, &reordered))).is_empty());
        let mut changed = identity.clone();
        changed.storage.as_mut().unwrap().engine = "ReplacingMergeTree".to_owned();
        assert_eq!(
            drift_reasons(&changed, "SELECT 1", Some((&hash, &metadata))),
            ["storage"]
        );
        changed.relation_name = "other".to_owned();
        assert_eq!(
            drift_reasons(&changed, "SELECT 1", Some((&hash, &metadata))),
            ["storage", "identity"]
        );
        assert_eq!(
            drift_reasons(&identity, "SELECT 1", Some((&hash, "[1]"))),
            ["storage", "identity"]
        );
        let view = ModelIdentity {
            storage: None,
            kind: "view".to_owned(),
            ..identity
        };
        // A view never reports storage drift, even against a table baseline.
        assert_eq!(
            drift_reasons(&view, "SELECT 1", Some((&hash, &metadata))),
            ["identity"]
        );
    }
}
