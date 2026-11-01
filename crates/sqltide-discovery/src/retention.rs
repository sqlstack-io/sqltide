//! Typed model and managed Kafka retention policies.

use crate::durations::parse_duration_seconds;
use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::value::{Kind, Value};

const MODEL_RETENTION_KEYS: [&str; 4] = [
    "duration",
    "timestamp_column",
    "cap_at_column",
    "when_missing",
];
const KAFKA_RETENTION_KEYS: [&str; 4] = ["duration", "timestamp", "fallback", "cap_at"];

/// A retention choice: unset, explicitly disabled, or a policy.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Retention<T> {
    Unset,
    Disabled,
    Policy(T),
}

/// Row retention for one model.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelRetention {
    pub duration_seconds: u64,
    pub timestamp_column: String,
    pub cap_at_column: Option<String>,
    /// `error` or `skip`.
    pub when_missing: String,
}

/// Retention for one managed Kafka source.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KafkaRetention {
    pub duration_seconds: u64,
    pub timestamp: String,
    pub fallback: String,
    pub cap_at: Option<String>,
}

fn error(message: String) -> SpecError {
    SpecError::new(ErrorKind::RetentionConfig, message)
}

/// The mapping of a retention choice, or the choice when it is unset or disabled.
fn retention_mapping<'a, T>(
    value: Option<&'a Value>,
    allowed: &[&str],
    field_path: &str,
) -> Result<&'a Value, SpecResult<Retention<T>>> {
    let Some(value) = value.filter(|value| !value.is_none()) else {
        return Err(Ok(Retention::Unset));
    };
    match value.kind {
        Kind::Bool(false) => return Err(Ok(Retention::Disabled)),
        Kind::Dict(_) | Kind::Mapping(_) => {}
        _ => {
            return Err(Err(error(format!(
                "{field_path} must be false or a mapping"
            ))));
        }
    }
    let mut invalid: Vec<String> = value
        .mapping_entries()
        .unwrap_or_default()
        .iter()
        .filter(|(key, _)| !key.as_str().is_some_and(|key| allowed.contains(&key)))
        .map(|(key, _)| key.python_str())
        .collect();
    invalid.sort();
    if !invalid.is_empty() {
        return Err(Err(error(format!(
            "{field_path} contains unsupported keys: {}",
            invalid.join(", ")
        ))));
    }
    Ok(value)
}

/// Python `parse_model_retention`.
///
/// # Errors
///
/// Returns a retention error naming the field.
pub fn parse_model_retention(
    value: Option<&Value>,
    field_path: &str,
) -> SpecResult<Retention<ModelRetention>> {
    let mapping: &Value = match retention_mapping(value, &MODEL_RETENTION_KEYS, field_path) {
        Ok(mapping) => mapping,
        Err(choice) => return choice,
    };
    let timestamp_column: String = column(
        mapping.get("timestamp_column"),
        &format!("{field_path}.timestamp_column"),
    )?;
    let cap_at_column: Option<String> = match mapping.get("cap_at_column") {
        Some(cap) if !cap.is_none() => {
            Some(column(Some(cap), &format!("{field_path}.cap_at_column"))?)
        }
        _ => None,
    };
    let when_missing: String = match mapping.get("when_missing") {
        None => "error".to_owned(),
        Some(value) => match value.as_str() {
            Some(text @ ("error" | "skip")) => text.to_owned(),
            _ => {
                return Err(error(format!(
                    "{field_path}.when_missing must be 'error' or 'skip'"
                )));
            }
        },
    };
    Ok(Retention::Policy(ModelRetention {
        duration_seconds: duration(mapping.get("duration"), &format!("{field_path}.duration"))?,
        timestamp_column,
        cap_at_column,
        when_missing,
    }))
}

/// Python `parse_kafka_retention`.
///
/// # Errors
///
/// Returns a retention error naming the field.
pub fn parse_kafka_retention(
    value: Option<&Value>,
    field_path: &str,
) -> SpecResult<Retention<KafkaRetention>> {
    let mapping: &Value = match retention_mapping(value, &KAFKA_RETENTION_KEYS, field_path) {
        Ok(mapping) => mapping,
        Err(choice) => return choice,
    };
    Ok(Retention::Policy(KafkaRetention {
        duration_seconds: duration(mapping.get("duration"), &format!("{field_path}.duration"))?,
        timestamp: choice(
            mapping.get("timestamp"),
            "broker",
            &format!("{field_path}.timestamp"),
        )?,
        fallback: choice(
            mapping.get("fallback"),
            "landed",
            &format!("{field_path}.fallback"),
        )?,
        cap_at: match mapping.get("cap_at") {
            Some(cap) if !cap.is_none() => Some(choice(
                Some(cap),
                "landed",
                &format!("{field_path}.cap_at"),
            )?),
            _ => None,
        },
    }))
}

fn duration(value: Option<&Value>, field_path: &str) -> SpecResult<u64> {
    parse_duration_seconds(value.and_then(Value::as_str), field_path, false)
        .map_err(|failure| failure.reraise(ErrorKind::RetentionConfig))
}

/// An unqualified `[A-Za-z_][A-Za-z0-9_]*` column name.
fn column(value: Option<&Value>, field_path: &str) -> SpecResult<String> {
    match value.and_then(Value::as_str) {
        Some(name) if is_identifier(name) => Ok(name.to_owned()),
        _ => Err(error(format!(
            "{field_path} must be an unqualified column name"
        ))),
    }
}

/// Whether `name` is `[A-Za-z_][A-Za-z0-9_]*`.
#[must_use]
pub fn is_identifier(name: &str) -> bool {
    let mut characters = name.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// The one allowed reference, defaulting when absent.
fn choice(value: Option<&Value>, allowed: &str, field_path: &str) -> SpecResult<String> {
    match value {
        None => Ok(allowed.to_owned()),
        Some(value) if value.as_str() == Some(allowed) => Ok(allowed.to_owned()),
        Some(_) => Err(error(format!("{field_path} must be '{allowed}'"))),
    }
}
