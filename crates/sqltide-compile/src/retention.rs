//! Render normalized retention policies into ClickHouse TTL expressions.

use std::collections::HashMap;

/// A Kafka retention policy over the fixed managed replay columns.
pub struct KafkaRetention {
    pub duration_seconds: i128,
    pub fallback_landed: bool,
    pub cap_at_landed: bool,
}

/// A model retention policy over the model's output columns.
pub struct ModelRetention<'a> {
    pub duration_seconds: i128,
    pub timestamp_column: &'a str,
    pub cap_at_column: Option<&'a str>,
    pub skip_when_missing: bool,
}

/// TTL expression over `_replay_timestamp`, optionally falling back to or capped at landing time.
#[must_use]
pub fn render_kafka_retention(policy: &KafkaRetention) -> String {
    let mut timestamp: String = "_replay_timestamp".to_owned();
    if policy.fallback_landed {
        timestamp = format!("ifNull({timestamp}, _replay_landed_at)");
    }
    if policy.cap_at_landed {
        timestamp = format!("least({timestamp}, _replay_landed_at)");
    }
    format!("{timestamp} + {}", interval(policy.duration_seconds))
}

/// TTL expression for one model, or `None` when skippable required columns are missing.
///
/// # Errors
///
/// Returns the compile error message when required columns are missing and the policy does
/// not skip, or when a required column is not a non-null `Date` or `DateTime` type.
pub fn render_model_retention(
    policy: &ModelRetention<'_>,
    available_columns: &HashMap<String, String>,
    model_name: &str,
) -> Result<Option<String>, String> {
    let columns: Vec<&str> = std::iter::once(policy.timestamp_column)
        .chain(policy.cap_at_column)
        .collect();
    let missing: Vec<&str> = columns
        .iter()
        .copied()
        .filter(|column| !available_columns.contains_key(*column))
        .collect();
    if !missing.is_empty() {
        if policy.skip_when_missing {
            return Ok(None);
        }
        return Err(format!(
            "Model '{model_name}' retention requires missing output columns: {}",
            missing.join(", ")
        ));
    }
    let incompatible: Vec<String> = columns
        .iter()
        .filter_map(|column| {
            let column_type: &str = &available_columns[*column];
            (!is_timestamp_type(column_type)).then(|| format!("{column} ({column_type})"))
        })
        .collect();
    if !incompatible.is_empty() {
        return Err(format!(
            "Model '{model_name}' retention columns must be non-null Date or DateTime values: {}",
            incompatible.join(", ")
        ));
    }
    let timestamp: String = match policy.cap_at_column {
        Some(cap_at) => format!("least({}, {cap_at})", policy.timestamp_column),
        None => policy.timestamp_column.to_owned(),
    };
    Ok(Some(format!(
        "{timestamp} + {}",
        interval(policy.duration_seconds)
    )))
}

/// The largest whole unit of days, hours, or minutes, else seconds.
fn interval(duration_seconds: i128) -> String {
    for (unit, seconds) in [("DAY", 86_400), ("HOUR", 3_600), ("MINUTE", 60)] {
        if duration_seconds % seconds == 0 {
            return format!("INTERVAL {} {unit}", duration_seconds / seconds);
        }
    }
    format!("INTERVAL {duration_seconds} SECOND")
}

fn is_timestamp_type(column_type: &str) -> bool {
    !column_type.starts_with("Nullable(")
        && (matches!(column_type, "Date" | "Date32") || column_type.starts_with("DateTime"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_largest_whole_interval_and_landed_fallbacks() {
        let policy = KafkaRetention {
            duration_seconds: 7_200,
            fallback_landed: true,
            cap_at_landed: true,
        };
        assert_eq!(
            render_kafka_retention(&policy),
            "least(ifNull(_replay_timestamp, _replay_landed_at), _replay_landed_at) + INTERVAL 2 HOUR"
        );
        assert_eq!(interval(90), "INTERVAL 90 SECOND");
        assert_eq!(interval(-3_600), "INTERVAL -1 HOUR");
    }

    #[test]
    fn rejects_nullable_and_reports_missing_in_policy_order() {
        let policy = ModelRetention {
            duration_seconds: 86_400,
            timestamp_column: "b",
            cap_at_column: Some("a"),
            skip_when_missing: false,
        };
        let mut columns: HashMap<String, String> = HashMap::new();
        assert_eq!(
            render_model_retention(&policy, &columns, "m"),
            Err("Model 'm' retention requires missing output columns: b, a".to_owned())
        );
        columns.insert("b".to_owned(), "Nullable(DateTime)".to_owned());
        columns.insert("a".to_owned(), "Date32".to_owned());
        assert_eq!(
            render_model_retention(&policy, &columns, "m"),
            Err(
                "Model 'm' retention columns must be non-null Date or DateTime values: \
                 b (Nullable(DateTime))"
                    .to_owned()
            )
        );
        columns.insert("b".to_owned(), "DateTime64(3)".to_owned());
        assert_eq!(
            render_model_retention(&policy, &columns, "m"),
            Ok(Some("least(b, a) + INTERVAL 1 DAY".to_owned()))
        );
    }
}
