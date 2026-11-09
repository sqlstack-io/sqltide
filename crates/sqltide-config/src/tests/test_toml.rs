//! TOML loading checked against outcomes recorded from CPython 3.12's `tomllib`: the project's
//! own config files, edge cases of each value type, every distinct failure a differential fuzzer
//! reached, and hostile nesting.

use serde_json::{Value as Json, json};

use crate::errors::ConfigError;
use crate::models::{ConfigDate, ConfigTime, ConfigValue};
use crate::toml::main::load_toml::load_toml;

/// Cases with the `tomllib` outcome (a typed value, an error, or the class of a Python exception
/// that was not a TOML error) and how the native outcome must relate to it: `exact`, `rejects`
/// (both reject, wording may differ), or a reviewed `difference` where the native loader accepts
/// or rejects what `tomllib` did not.
const CORPUS: &str = include_str!("fixtures/toml_corpus.json");
/// Reviewed ways the native loader reads TOML differently from `tomllib`.
const DIFFERENCES: [&str; 4] = [
    "integer-beyond-64-bit",
    "float-beyond-binary64",
    "byte-order-mark",
    "nesting-limit",
];

fn date(value: ConfigDate) -> String {
    format!("{:04}-{:02}-{:02}", value.year, value.month, value.day)
}

/// `datetime.time.isoformat()`: microseconds only when there are some.
fn time(value: ConfigTime) -> String {
    let clock = format!("{:02}:{:02}:{:02}", value.hour, value.minute, value.second);
    match value.microsecond {
        0 => clock,
        microsecond => format!("{clock}.{microsecond:06}"),
    }
}

/// The typed form the corpus records Python values in.
fn typed(value: &ConfigValue) -> Json {
    match value {
        ConfigValue::Bool(flag) => json!(["bool", flag]),
        ConfigValue::Integer(number) => json!(["int", number.to_string()]),
        ConfigValue::Float(number) if number.is_nan() => json!(["float", "nan"]),
        ConfigValue::Float(number) => json!(["float", number.to_bits().to_string()]),
        ConfigValue::String(text) => json!(["str", text]),
        ConfigValue::Date(value) => json!(["date", date(*value)]),
        ConfigValue::Time(value) => json!(["time", time(*value)]),
        ConfigValue::DateTime(value) => json!([
            "datetime",
            format!("{}T{}", date(value.date), time(value.time)),
            value.utc_offset_seconds,
        ]),
        ConfigValue::List(items) => json!(["list", items.iter().map(typed).collect::<Vec<_>>()]),
        ConfigValue::Map(entries) => json!([
            "dict",
            entries
                .iter()
                .map(|(key, item)| json!([key, typed(item)]))
                .collect::<Vec<_>>(),
        ]),
    }
}

fn outcome(result: Result<ConfigValue, ConfigError>) -> Json {
    match result {
        Ok(document) => json!({ "value": typed(&document) }),
        Err(error) => json!({ "error": error.message }),
    }
}

/// Why the native outcome breaks the case's contract, if it does.
fn violation(case: &Json, actual: &Json) -> Option<String> {
    let accepted: bool = actual.get("value").is_some();
    if let Some(difference) = case.get("difference").and_then(Json::as_str)
        && !DIFFERENCES.contains(&difference)
    {
        return Some(format!("unreviewed difference {difference}"));
    }
    match case["match"].as_str().expect("each case has a match mode") {
        "exact" => (actual != &case["python"]).then(|| "differs from tomllib".to_owned()),
        "rejects" => accepted.then(|| "accepts what tomllib rejected".to_owned()),
        "rejects-accepted" => accepted.then(|| "accepts, yet the difference rejects".to_owned()),
        "accepts-rejected" => (!accepted).then(|| "rejects, yet the difference accepts".to_owned()),
        mode => Some(format!("unknown match mode {mode}")),
    }
}

#[test]
fn given_recorded_tomllib_outcomes_when_loading_then_outcomes_keep_their_contract() {
    let cases: Vec<Json> = serde_json::from_str(CORPUS).expect("the corpus is JSON");

    let violations: Vec<String> = cases
        .iter()
        .filter_map(|case| {
            let contents: &str = case["toml"].as_str().expect("each case has TOML");
            let actual: Json = outcome(load_toml(contents));
            violation(case, &actual).map(|reason| {
                format!(
                    "{contents:?}: {reason}\n  python {}\n  actual {actual}",
                    case["python"]
                )
            })
        })
        .collect();

    assert!(cases.len() > 500, "the corpus has {} cases", cases.len());
    assert!(
        violations.is_empty(),
        "{} of {} cases break their contract:\n{}",
        violations.len(),
        cases.len(),
        violations.join("\n")
    );
}
