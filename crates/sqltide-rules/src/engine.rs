//! SQLClean's published engine, in process. SQLTide owns authored text and compiler proof.

use std::collections::BTreeSet;

use fensu_policy::lifecycle::models::{
    ApplySuppressionsRequest, ExactSuppression, Finding, ScopedIgnore,
};
use fensu_policy::policy::types::RuleCodeGrammar;
use sqlclean::sql_lint::main::{catalogue, cross_join_subject, default_rules};
use sqltide_sqltext::sql_scan::{
    ScanResult, extract_references, find_matching_parenthesis, find_next_unquoted_character,
    skip_block_comment, skip_line_comment, skip_quoted_text,
};

/// The family selector that selects every SQLClean Rule.
pub const SQLCLEAN_FAMILY: &str = "SQLCLEAN";

/// Every published Rule code in catalogue order, with whether SQLClean enables it by default.
#[must_use]
pub fn rule_codes() -> Vec<(&'static str, bool)> {
    let defaults = default_rules::default_rules();
    catalogue::catalogue()
        .into_iter()
        .map(|rule| (rule.code, defaults.contains(&rule.code)))
        .collect()
}

/// The relation and optional CTE a cross-join finding's message names.
#[must_use]
pub fn cross_join_subject(message: &str) -> Option<(String, Option<String>)> {
    cross_join_subject::cross_join_subject(message)
}

struct SqlCleanGrammar;

impl RuleCodeGrammar for SqlCleanGrammar {
    fn rule_code_is_exact(&self, value: &str) -> bool {
        catalogue::catalogue().iter().any(|rule| rule.code == value)
    }

    fn rule_selector_is_valid(&self, value: &str) -> bool {
        value == SQLCLEAN_FAMILY || self.rule_code_is_exact(value)
    }
}

/// The indices of `findings` SQLBuild's published exact and scoped policy lifecycle keeps.
///
/// # Errors
///
/// Returns the lifecycle's refusal, such as a stale or malformed exception.
pub fn apply_policy(
    findings: Vec<Finding>,
    evaluated_codes: &[String],
    suppressions: &[ExactSuppression],
    scoped_ignores: &[ScopedIgnore],
) -> Result<Vec<usize>, String> {
    let originals = findings.clone();
    let result = fensu_policy::apply_suppressions(ApplySuppressionsRequest {
        findings,
        evaluated_codes,
        suppressions,
        scoped_ignores,
        grammar: &SqlCleanGrammar,
    })
    .map_err(|error| error.to_string())?;
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

/// Template ranges for layout protection, using the compiler's lexical rules. A macro inside a
/// relation call's arguments is covered by the outer site.
///
/// # Errors
///
/// Returns the scan failure for unclosed syntax.
pub fn template_sites(chars: &[char]) -> ScanResult<Vec<(usize, usize)>> {
    let mut sites: Vec<(usize, usize)> = extract_references(chars)?
        .into_iter()
        .map(|reference| (reference.span.start, reference.span.end))
        .collect();
    let mut cursor = 0;
    while let Some(start) = find_next_unquoted_character(chars, '@', cursor)? {
        let mut end = start + 1;
        if chars.get(end) == Some(&'\'') {
            end = skip_quoted_text(chars, end)?;
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
                end = find_matching_parenthesis(chars, open, "SQL template call")? + 1;
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
    let mut outer: Vec<(usize, usize)> = Vec::new();
    for site in sites {
        if outer.last().is_none_or(|previous| site.0 >= previous.1) {
            outer.push(site);
        }
    }
    Ok(outer)
}

/// Comment-only inline suppression directives. Strings and quoted identifiers are not comments.
///
/// # Errors
///
/// Returns the scan failure for unclosed syntax.
pub fn suppressions(chars: &[char]) -> ScanResult<Vec<(usize, usize)>> {
    let mut sites = Vec::new();
    let mut cursor = 0;
    while cursor < chars.len() {
        if matches!(chars[cursor], '\'' | '"' | '`') {
            cursor = skip_quoted_text(chars, cursor)?;
            continue;
        }
        let rest = &chars[cursor..];
        let end = if rest.starts_with(&['/', '*']) {
            Some(skip_block_comment(chars, cursor)?)
        } else if rest.starts_with(&['-', '-']) || rest.starts_with(&['#']) {
            Some(skip_line_comment(chars, cursor))
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
