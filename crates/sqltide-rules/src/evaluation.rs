//! Evaluate published SQLClean Rules and keep authored diagnostics and fix ownership.

use serde_json::{Map, Value, json};
use sqlclean::sql_lint::main::batch_engine::lint_batch_json;

use crate::bodies::{LintBody, source_range};
use crate::config::RulesConfig;
use crate::engine::suppressions;
use crate::policy::apply_rule_policy;
use crate::{RuleFile, RulesFailure, line_and_column};

const SUPPRESSION_CODE: &str = "SQTSUPPRESSION000";
const QUALIFY_CODE: &str = "SQLCLEAN003";
const SNOWFLAKE_DIALECT: &str = "snowflake";
const SAFE_FIX_CODES: [&str; 8] = [
    "SQLCLEAN002",
    "SQLCLEAN003",
    "SQLCLEAN005",
    "SQLCLEAN006",
    "SQLCLEAN008",
    "SQLCLEAN017",
    "SQLCLEAN025",
    "SQLCLEAN042",
];
/// Fixture harness CTE names: ref and source inputs, expectations, assertions and macro tests.
const REF_CTE_PREFIX: &str = "__ref__";
const SOURCE_CTE_PREFIX: &str = "__source__";
const FIXTURE_CTE_PREFIXES: [&str; 6] = [
    REF_CTE_PREFIX,
    SOURCE_CTE_PREFIX,
    "__expected__",
    "__assert__",
    "__macro_actual__",
    "__macro_expected__",
];

/// A diagnostic in an authored file and an optional edit proven to touch only authored model SQL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuleFinding {
    pub file: usize,
    pub code: String,
    pub message: String,
    pub remediation: String,
    pub start: i64,
    pub end: i64,
    pub line: i64,
    pub column: i64,
    pub end_line: i64,
    pub end_column: i64,
    pub replacement: Option<String>,
    pub fix_start: Option<i64>,
    pub fix_end: Option<i64>,
}

fn finding(
    files: &[RuleFile],
    file: usize,
    code: String,
    message: String,
    remediation: String,
    (start, end): (i64, i64),
) -> RuleFinding {
    let contents = &files[file].contents;
    let (line, column) = line_and_column(contents, start);
    let (end_line, end_column) = line_and_column(contents, end);
    RuleFinding {
        file,
        code,
        message,
        remediation,
        start,
        end,
        line,
        column,
        end_line,
        end_column,
        replacement: None,
        fix_start: None,
        fix_end: None,
    }
}

fn request(body: &LintBody, selected: &[String], config: &RulesConfig, dialect: &str) -> Value {
    let mut request: Map<String, Value> = Map::new();
    request.insert("version".into(), json!(1));
    request.insert("sql".into(), json!(body.sql.iter().collect::<String>()));
    request.insert("dialect".into(), json!(dialect));
    request.insert("enabled_rules".into(), json!(selected));
    request.insert("dependency_identifiers".into(), json!(body.dependencies));
    request.insert(
        "external_identifiers".into(),
        json!(body.external_identifiers),
    );
    request.insert(
        "relation_names".into(),
        Value::Object(
            body.relation_names
                .iter()
                .map(|(token, name)| (token.clone(), json!(name)))
                .collect(),
        ),
    );
    request.insert(
        "sql_macro_outputs".into(),
        Value::Array(
            body.macro_outputs
                .iter()
                .map(|(start, end)| json!({"start": start, "end": end}))
                .collect(),
        ),
    );
    request.insert("config_file".into(), json!("sqltide_project.toml"));
    for (key, value) in &config.thresholds {
        request.insert(key.clone(), json!(value));
    }
    if body.fixture {
        request.insert(
            "fixture_harness".into(),
            json!({
                "cte_prefixes": FIXTURE_CTE_PREFIXES,
                "input_cte_prefixes": [REF_CTE_PREFIX, SOURCE_CTE_PREFIX],
            }),
        );
    }
    Value::Object(request)
}

fn text_field(raw: &Value, key: &str) -> String {
    raw[key].as_str().unwrap_or_default().to_owned()
}

fn diagnostic_finding(
    files: &[RuleFile],
    body: &LintBody,
    raw: &Value,
    dialect: &str,
) -> Result<RuleFinding, RulesFailure> {
    let offset = |value: &Value| value.as_i64().unwrap_or_default();
    let range =
        source_range(body, offset(&raw["start"]), offset(&raw["end"]), false).ok_or_else(|| {
            RulesFailure::Rules("SQLClean diagnostic could not be mapped to authored SQL".into())
        })?;
    let code = text_field(raw, "code");
    let mut result = finding(
        files,
        body.file,
        code.clone(),
        text_field(raw, "message"),
        text_field(raw, "remediation"),
        range,
    );
    if let Some(fix) = raw.get("fix").filter(|fix| !fix.is_null())
        && body.model
        && SAFE_FIX_CODES.contains(&code.as_str())
        && (code != QUALIFY_CODE || dialect == SNOWFLAKE_DIALECT)
        && let Some((fix_start, fix_end)) =
            source_range(body, offset(&fix["start"]), offset(&fix["end"]), true)
    {
        result.fix_start = Some(fix_start);
        result.fix_end = Some(fix_end);
        result.replacement = Some(text_field(fix, "replacement"));
    }
    Ok(result)
}

/// All findings for `bodies`, sorted by path, start and code. Selecting no Rules skips the SQL
/// parser entirely. `check_stale` reports exact exceptions that no longer match a finding.
///
/// # Errors
///
/// Returns an engine refusal, SQL SQLClean cannot analyze, a policy refusal or a scan failure.
pub fn evaluate_rules(
    files: &[RuleFile],
    bodies: &[LintBody],
    config: &RulesConfig,
    dialect: &str,
    check_stale: bool,
) -> Result<Vec<RuleFinding>, RulesFailure> {
    let selected = config.selected_rules();
    let active: Vec<&LintBody> = bodies.iter().filter(|body| body.enabled).collect();
    let mut findings: Vec<RuleFinding> = Vec::new();
    if !selected.is_empty() {
        let requests: Vec<Value> = active
            .iter()
            .map(|body| request(body, &selected, config, dialect))
            .collect();
        let requests = serde_json::to_string(&requests)
            .map_err(|error| RulesFailure::Value(error.to_string()))?;
        let responses: Vec<Value> =
            serde_json::from_str(&lint_batch_json(&requests).map_err(RulesFailure::Value)?)
                .map_err(|error| RulesFailure::Value(error.to_string()))?;
        for (body, response) in active.iter().zip(&responses) {
            if let Some(error) = response.get("Err") {
                let error = error
                    .as_str()
                    .map_or_else(|| error.to_string(), str::to_owned);
                return Err(RulesFailure::Rules(format!(
                    "{}: SQLClean could not analyze SQL: {error}",
                    files[body.file].path
                )));
            }
            for raw in response["Ok"]["diagnostics"]
                .as_array()
                .into_iter()
                .flatten()
            {
                findings.push(diagnostic_finding(files, body, raw, dialect)?);
            }
        }
    }
    let stale_codes: &[String] = if check_stale { &selected } else { &[] };
    let mut findings = apply_rule_policy(findings, config, stale_codes, files)?;
    if !selected.is_empty() {
        let mut seen: Vec<usize> = Vec::new();
        for body in &active {
            if seen.contains(&body.file) {
                continue;
            }
            seen.push(body.file);
            for (start, end) in suppressions(&files[body.file].contents)? {
                let range = (
                    i64::try_from(start).unwrap_or(i64::MAX),
                    i64::try_from(end).unwrap_or(i64::MAX),
                );
                findings.push(finding(
                    files,
                    body.file,
                    SUPPRESSION_CODE.to_owned(),
                    "Inline Rule suppressions are not supported".to_owned(),
                    "Declare a reviewed [[rules.rule_exceptions]] entry with rule, path and \
                     reason in sqltide_project.toml."
                        .to_owned(),
                    range,
                ));
            }
        }
    }
    findings.sort_by(|left, right| {
        (&files[left.file].path, left.start, &left.code).cmp(&(
            &files[right.file].path,
            right.start,
            &right.code,
        ))
    });
    Ok(findings)
}
