//! Layout-only SQLClean formatting with opaque template text and all native safety guards.

use serde_json::{Map, Value, json};
use sqlclean::sql_lint::main::formatter::format_json;
use sqltide_sqltext::python_text::python_isspace;

use crate::bodies::{LintBody, authored_bodies, protected_sql};
use crate::{RuleFile, RulesFailure};

/// The file with only its SQL body ranges formatted; headers and surrounding whitespace keep
/// their exact text.
///
/// # Errors
///
/// Returns a missing header, a header or scan failure, or a formatter refusal.
pub fn formatted_file(
    files: &[RuleFile],
    file: usize,
    keyword: &str,
    dialect: &str,
) -> Result<String, RulesFailure> {
    let bodies = authored_bodies(files, file, keyword)?;
    if bodies.is_empty() {
        return Err(RulesFailure::Rules(format!(
            "{}: expected a {keyword} header",
            files[file].path
        )));
    }
    let mut candidate: Vec<char> = files[file].contents.clone();
    for body in bodies.iter().rev() {
        let formatted: Vec<char> = formatted_body(files, body, dialect)?.chars().collect();
        let (start, end) = (
            bound(body.start, candidate.len()),
            bound(body.end, candidate.len()),
        );
        candidate.splice(start..end.max(start), formatted);
    }
    Ok(candidate.into_iter().collect())
}

fn bound(index: i64, length: usize) -> usize {
    usize::try_from(index).unwrap_or(0).min(length)
}

fn formatted_body(
    files: &[RuleFile],
    body: &LintBody,
    dialect: &str,
) -> Result<String, RulesFailure> {
    let path = &files[body.file].path;
    let (sql, replacements, _) = protected_sql(&body.sql, &[])?;
    let widths: Map<String, Value> = replacements
        .iter()
        .map(|(token, original)| (token.clone(), json!(original.chars().count())))
        .collect();
    let request = json!({
        "version": 1,
        "sql": sql.iter().collect::<String>(),
        "dialect": dialect,
        "token_widths": widths,
    });
    let response: Value =
        serde_json::from_str(&format_json(&request.to_string()).map_err(RulesFailure::Value)?)
            .map_err(|error| RulesFailure::Value(error.to_string()))?;
    if response["formatted"].as_bool() != Some(true) {
        let reason = response.get("reason").map_or_else(
            || "unknown reason".to_owned(),
            |reason| {
                reason
                    .as_str()
                    .map_or_else(|| reason.to_string(), str::to_owned)
            },
        );
        return Err(RulesFailure::Rules(format!(
            "{path}: formatter declined: {reason}"
        )));
    }
    let mut formatted: String = response["sql"]
        .as_str()
        .unwrap_or_default()
        .trim_end_matches(python_isspace)
        .to_owned();
    if files[body.file]
        .contents
        .windows(2)
        .any(|pair| pair == ['\r', '\n'])
    {
        formatted = formatted.replace("\r\n", "\n").replace('\n', "\r\n");
    }
    for (token, original) in &replacements {
        if formatted.matches(token.as_str()).count() != 1 {
            return Err(RulesFailure::Rules(format!(
                "{path}: formatter did not preserve an opaque template"
            )));
        }
        formatted = formatted.replace(token.as_str(), original);
    }
    Ok(formatted)
}
