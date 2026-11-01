//! Source freshness thresholds written as duration literals.

use std::cmp::Ordering;

use crate::durations::{compare_durations, split_duration};
use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::value::{Kind, Value};

const FRESHNESS_KEYS: [&str; 2] = ["warn_after", "error_after"];

/// Freshness thresholds, kept as authored.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Freshness {
    pub warn_after: Option<String>,
    pub error_after: Option<String>,
}

fn error(message: String) -> SpecError {
    SpecError::new(ErrorKind::PipelineDiscovery, message)
}

/// Python `parse_freshness_policy`; `None` when unset.
///
/// # Errors
///
/// Returns a discovery error naming the file and label.
pub fn parse_freshness_policy(
    payload: Option<&Value>,
    label: &str,
    file_path: &str,
) -> SpecResult<Option<Freshness>> {
    let Some(payload) = payload.filter(|payload| !payload.is_none()) else {
        return Ok(None);
    };
    let Kind::Dict(entries) = &payload.kind else {
        return Err(error(format!(
            "'{file_path}' {label} must be a mapping when set"
        )));
    };
    if entries.iter().any(|(key, _)| key.as_str().is_none()) {
        return Err(error(format!(
            "'{file_path}' {label} must be a mapping when set"
        )));
    }
    let mut unknown: Vec<&str> = entries
        .iter()
        .filter_map(|(key, _)| key.as_str())
        .filter(|key| !FRESHNESS_KEYS.contains(key))
        .collect();
    unknown.sort_unstable();
    unknown.dedup();
    if !unknown.is_empty() {
        return Err(error(format!(
            "'{file_path}' {label} has unknown keys: {}",
            unknown.join(", ")
        )));
    }
    let threshold = |key: &str| -> SpecResult<Option<String>> {
        match payload.get(key) {
            None => Ok(None),
            Some(value) if value.is_none() => Ok(None),
            Some(value) => match value.as_str() {
                Some(text) if split_duration(text).is_some() => Ok(Some(text.to_owned())),
                _ => Err(error(format!(
                    "'{file_path}' {label}.{key} must be a duration like '15m', '2h', or '1d'"
                ))),
            },
        }
    };
    let warn_after: Option<String> = threshold("warn_after")?;
    let error_after: Option<String> = threshold("error_after")?;
    if warn_after.is_none() && error_after.is_none() {
        return Err(error(format!(
            "'{file_path}' {label} must set at least one of warn_after or error_after"
        )));
    }
    if let (Some(warn), Some(fail)) = (&warn_after, &error_after)
        && compare_durations(warn, fail) == Some(Ordering::Greater)
    {
        return Err(error(format!(
            "'{file_path}' {label}.warn_after must not exceed error_after"
        )));
    }
    Ok(Some(Freshness {
        warn_after,
        error_after,
    }))
}
