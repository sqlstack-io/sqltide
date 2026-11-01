//! SQLClean's published engine, in process. SQLTide owns authored text and compiler proof.

use std::collections::BTreeSet;

use fensu_policy::lifecycle::models::{
    ApplySuppressionsRequest, ExactSuppression, Finding, ScopedIgnore,
};
use fensu_policy::policy::types::RuleCodeGrammar;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::Deserialize;
use sqlclean::sql_lint::main::{batch_engine, catalogue, default_rules, formatter};
use sqltide_sqltext::sql_scan::{
    extract_references, find_matching_parenthesis, find_next_unquoted_character,
    skip_block_comment, skip_line_comment, skip_quoted_text,
};

use crate::sql_scan::raise;

#[pyfunction]
fn sqlclean_lint_batch(py: Python<'_>, request: String) -> PyResult<String> {
    py.detach(|| batch_engine::lint_batch_json(&request))
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn sqlclean_format(py: Python<'_>, request: String) -> PyResult<String> {
    py.detach(|| formatter::format_json(&request))
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn sqlclean_rules() -> Vec<(&'static str, &'static str, &'static str, bool)> {
    let defaults = default_rules::default_rules();
    catalogue::catalogue()
        .into_iter()
        .map(|rule| {
            (
                rule.code,
                rule.message,
                rule.remediation,
                defaults.contains(&rule.code),
            )
        })
        .collect()
}

#[pyfunction]
fn sqlclean_cross_join_subject(message: &str) -> Option<(String, Option<String>)> {
    sqlclean::sql_lint::main::cross_join_subject::cross_join_subject(message)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyRequest {
    findings: Vec<Finding>,
    evaluated_codes: Vec<String>,
    suppressions: Vec<ExactSuppression>,
    scoped_ignores: Vec<ScopedIgnore>,
}

struct SqlCleanGrammar;

impl RuleCodeGrammar for SqlCleanGrammar {
    fn rule_code_is_exact(&self, value: &str) -> bool {
        catalogue::catalogue().iter().any(|rule| rule.code == value)
    }

    fn rule_selector_is_valid(&self, value: &str) -> bool {
        value == "SQLCLEAN" || self.rule_code_is_exact(value)
    }
}

/// Return original finding indices after SQLBuild's published exact/scoped policy lifecycle.
#[pyfunction]
fn sqlclean_apply_policy(request: &str) -> PyResult<Vec<usize>> {
    let request: PolicyRequest =
        serde_json::from_str(request).map_err(|error| PyValueError::new_err(error.to_string()))?;
    let originals = request.findings.clone();
    let result = fensu_policy::apply_suppressions(ApplySuppressionsRequest {
        findings: request.findings,
        evaluated_codes: &request.evaluated_codes,
        suppressions: &request.suppressions,
        scoped_ignores: &request.scoped_ignores,
        grammar: &SqlCleanGrammar,
    })
    .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let kept: BTreeSet<_> = result
        .findings
        .iter()
        .map(|finding| (&finding.code, &finding.path, &finding.symbol))
        .collect();
    Ok(originals
        .iter()
        .enumerate()
        .filter_map(|(index, finding)| {
            kept.contains(&(&finding.code, &finding.path, &finding.symbol))
                .then_some(index)
        })
        .collect())
}

/// Template ranges for layout protection, using the compiler's lexical rules.
#[pyfunction]
fn sqlclean_template_sites(sql: &str) -> PyResult<Vec<(usize, usize)>> {
    let chars: Vec<char> = sql.chars().collect();
    let mut sites: Vec<(usize, usize)> = extract_references(&chars)
        .map_err(raise)?
        .into_iter()
        .map(|reference| (reference.span.start, reference.span.end))
        .collect();
    let mut cursor = 0;
    while let Some(start) = find_next_unquoted_character(&chars, '@', cursor).map_err(raise)? {
        let mut end = start + 1;
        if chars.get(end) == Some(&'\'') {
            end = skip_quoted_text(&chars, end).map_err(raise)?;
        } else {
            while chars
                .get(end)
                .is_some_and(|c| c.is_alphanumeric() || *c == '_')
            {
                end += 1;
            }
            let mut open = end;
            while chars.get(open).is_some_and(|c| c.is_whitespace()) {
                open += 1;
            }
            if chars.get(open) == Some(&'(') {
                end = find_matching_parenthesis(&chars, open, "SQL template call")
                    .map_err(raise)?
                    + 1;
                if chars.get(end) == Some(&'.') {
                    end += 1;
                    while chars
                        .get(end)
                        .is_some_and(|c| c.is_alphanumeric() || *c == '_')
                    {
                        end += 1;
                    }
                }
            }
        }
        if end > start + 1 {
            sites.push((start, end));
        }
        cursor = end;
    }
    sites.sort_unstable();
    // A macro may occur inside a relation call's arguments. Protect only the outer site.
    let mut outer: Vec<(usize, usize)> = Vec::new();
    for site in sites {
        if outer.last().is_none_or(|previous| site.0 >= previous.1) {
            outer.push(site);
        }
    }
    Ok(outer)
}

/// Comment-only inline suppression directives. Strings and quoted identifiers are not comments.
#[pyfunction]
fn sqlclean_suppressions(sql: &str) -> PyResult<Vec<(usize, usize)>> {
    let chars: Vec<char> = sql.chars().collect();
    let mut sites = Vec::new();
    let mut cursor = 0;
    while cursor < chars.len() {
        if matches!(chars[cursor], '\'' | '"' | '`') {
            cursor = skip_quoted_text(&chars, cursor).map_err(raise)?;
            continue;
        }
        let rest = &chars[cursor..];
        let end = if rest.starts_with(&['/', '*']) {
            Some(skip_block_comment(&chars, cursor).map_err(raise)?)
        } else if rest.starts_with(&['-', '-']) || rest.starts_with(&['#']) {
            Some(skip_line_comment(&chars, cursor))
        } else {
            None
        };
        if let Some(end) = end {
            let comment: String = chars[cursor..end]
                .iter()
                .collect::<String>()
                .to_ascii_lowercase();
            for marker in ["sqt:", "sqb:"] {
                if let Some(start) = comment.find(marker)
                    && comment[start + marker.len()..]
                        .trim_start()
                        .starts_with("ignore")
                {
                    sites.push((cursor, end));
                    break;
                }
            }
            cursor = end;
        } else {
            cursor += 1;
        }
    }
    Ok(sites)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(sqlclean_apply_policy, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_lint_batch, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_format, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_template_sites, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_suppressions, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_cross_join_subject, module)?)?;
    module.add_function(wrap_pyfunction!(sqlclean_rules, module)?)
}
