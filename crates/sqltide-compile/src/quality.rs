//! Stable SQL test and audit identities: canonical JSON payloads hashed with SHA-256.
//!
//! These hashes are persisted, so the JSON must match Python's
//! `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` byte for byte.

use sha2::{Digest, Sha256};
use sqltide_json::{JsonDialect, JsonKey, JsonValue, StdlibJsonOptions, dumps};

/// `(binding key, definition fingerprint, execution fingerprint)`.
pub type QualityFingerprints = (String, String, String);

/// One SQL test's identity inputs, with SQL already canonicalized.
pub struct TestIdentityInput<'a> {
    pub node_name: &'a str,
    pub mode: &'a str,
    pub assertion_names: Vec<&'a str>,
    pub expected_targets: Vec<&'a str>,
    /// `(name, canonical SQL)` in authored order.
    pub authored_ctes: Vec<(&'a str, &'a str)>,
    pub execution_sql: &'a str,
}

/// One SQL audit's identity inputs, with SQL already canonicalized.
pub struct AuditIdentityInput<'a> {
    pub node_name: &'a str,
    pub attached_column: Option<&'a str>,
    pub attached_model: Option<&'a str>,
    pub attachment_kind: &'a str,
    pub referenced_models: Vec<&'a str>,
    pub severity: &'a str,
    pub definition_sql: &'a str,
    pub execution_sql: &'a str,
}

fn text(value: &str) -> JsonValue {
    JsonValue::String(value.to_owned())
}

fn optional_text(value: Option<&str>) -> JsonValue {
    value.map_or(JsonValue::Null, text)
}

fn sorted_texts(mut values: Vec<&str>) -> JsonValue {
    values.sort_unstable();
    JsonValue::Array(values.into_iter().map(text).collect())
}

fn object(entries: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (JsonKey::from(key.to_owned()), value))
            .collect(),
    )
}

fn fingerprint(payload: &JsonValue) -> String {
    let mut options = StdlibJsonOptions::new(None).with_separators(",", ":");
    options.sort_keys = true;
    let canonical: String = dumps(payload, &JsonDialect::Stdlib(options))
        .expect("quality payloads hold only strings, arrays, objects, and nulls");
    format!("{:x}", Sha256::digest(canonical.as_bytes()))
}

fn sql_payload(sql: &str) -> JsonValue {
    object(vec![("sql", text(sql))])
}

/// The authored-definition and assembled-execution identity of one SQL test.
#[must_use]
pub fn test_identity(input: TestIdentityInput<'_>) -> QualityFingerprints {
    let binding = object(vec![
        ("assertion_names", sorted_texts(input.assertion_names)),
        ("expected_targets", sorted_texts(input.expected_targets)),
        ("mode", text(input.mode)),
        ("node_kind", text("test")),
        ("node_name", text(input.node_name)),
    ]);
    let definition = object(vec![(
        "authored_ctes",
        JsonValue::Array(
            input
                .authored_ctes
                .into_iter()
                .map(|(name, sql)| object(vec![("name", text(name)), ("sql", text(sql))]))
                .collect(),
        ),
    )]);
    (
        fingerprint(&binding),
        fingerprint(&definition),
        fingerprint(&sql_payload(input.execution_sql)),
    )
}

/// The target-neutral and target-resolved identity of one SQL audit.
#[must_use]
pub fn audit_identity(input: AuditIdentityInput<'_>) -> QualityFingerprints {
    let binding = object(vec![
        ("attached_column", optional_text(input.attached_column)),
        ("attached_model", optional_text(input.attached_model)),
        ("attachment_kind", text(input.attachment_kind)),
        ("node_kind", text("audit")),
        ("node_name", text(input.node_name)),
        ("referenced_models", sorted_texts(input.referenced_models)),
        ("severity", text(input.severity)),
    ]);
    (
        fingerprint(&binding),
        fingerprint(&sql_payload(input.definition_sql)),
        fingerprint(&sql_payload(input.execution_sql)),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `hashlib.sha256(json.dumps({"sql": "SELECT 1"}, sort_keys=True, separators=(",", ":"),
    /// ensure_ascii=True).encode()).hexdigest()`.
    #[test]
    fn hashes_python_canonical_json() {
        assert_eq!(
            fingerprint(&sql_payload("SELECT 1")),
            format!("{:x}", Sha256::digest(br#"{"sql":"SELECT 1"}"#))
        );
        assert_eq!(
            fingerprint(&sql_payload("é")),
            format!("{:x}", Sha256::digest(br#"{"sql":"\u00e9"}"#))
        );
    }
}
