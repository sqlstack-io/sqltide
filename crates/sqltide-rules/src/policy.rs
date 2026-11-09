//! SQLBuild-compatible exact exceptions and reasoned path ignores via the published policy.

use fensu_policy::lifecycle::models::{ExactSuppression, Finding, FindingSeverity, ScopedIgnore};

use crate::config::{RuleExemption, RulesConfig};
use crate::engine::{apply_policy, cross_join_subject};
use crate::evaluation::RuleFinding;
use crate::{RuleFile, RulesFailure};

fn symbol(entry: &RuleExemption) -> Option<String> {
    let relation = entry.relation.as_ref()?;
    let mut text = format!("relation {}", relation.to_lowercase());
    if let Some(cte) = &entry.cte {
        text.push_str(&format!(" in CTE {}", cte.to_lowercase()));
    }
    Some(text)
}

fn suppressions(config: &RulesConfig) -> Vec<ExactSuppression> {
    config
        .rule_exceptions
        .iter()
        .map(|entry| ExactSuppression {
            code: entry.rule.clone(),
            path: entry.path.clone(),
            symbol: symbol(entry),
            reason: entry.reason.clone(),
        })
        .collect()
}

fn scoped_ignores<'a>(
    ignores: impl Iterator<Item = &'a crate::config::RuleIgnore>,
) -> Vec<ScopedIgnore> {
    ignores
        .map(|entry| ScopedIgnore {
            selectors: entry.rules.clone(),
            paths: entry.paths.clone(),
            reason: entry.reason.clone(),
        })
        .collect()
}

/// The exact exception that covers a cross-join finding's relation, preferring one that also
/// names the CTE.
fn covering_symbol(
    finding: &RuleFinding,
    relative_path: &str,
    config: &RulesConfig,
) -> Option<String> {
    let (relation, cte) = cross_join_subject(&finding.message)?;
    let mut matching: Vec<&RuleExemption> = config
        .rule_exceptions
        .iter()
        .filter(|entry| {
            entry.rule == finding.code
                && relative_path == entry.path
                && entry
                    .relation
                    .as_ref()
                    .is_some_and(|name| name.eq_ignore_ascii_case(&relation))
                && entry.cte.as_ref().is_none_or(|name| {
                    cte.as_ref()
                        .is_some_and(|subject| name.eq_ignore_ascii_case(subject))
                })
        })
        .collect();
    matching.sort_by_key(|entry| entry.cte.is_none());
    matching.first().and_then(|entry| symbol(entry))
}

/// Reject a configuration whose exceptions or path ignores the policy lifecycle refuses.
pub(crate) fn validate_policy(config: &RulesConfig) -> Result<(), RulesFailure> {
    let ignores = scoped_ignores(
        config
            .rule_ignores
            .iter()
            .filter(|entry| !entry.paths.is_empty()),
    );
    if config.rule_exceptions.is_empty() && ignores.is_empty() {
        return Ok(());
    }
    apply_policy(Vec::new(), &[], &suppressions(config), &ignores).map_err(RulesFailure::Value)?;
    Ok(())
}

/// The findings the configured exceptions and ignores keep, in their original order. A non-empty
/// `selected` also reports exceptions that no longer match a finding.
pub(crate) fn apply_rule_policy(
    findings: Vec<RuleFinding>,
    config: &RulesConfig,
    selected: &[String],
    files: &[RuleFile],
) -> Result<Vec<RuleFinding>, RulesFailure> {
    if config.rule_exceptions.is_empty() && config.rule_ignores.is_empty() {
        return Ok(findings);
    }
    let mut policy_findings: Vec<Finding> = Vec::with_capacity(findings.len());
    for finding in &findings {
        let relative_path = files[finding.file]
            .relative_path
            .clone()
            .map_err(RulesFailure::Value)?;
        policy_findings.push(Finding {
            code: finding.code.clone(),
            symbol: covering_symbol(finding, &relative_path, config),
            path: relative_path,
            line: None,
            column: None,
            message: finding.message.clone(),
            remediation: None,
            severity: FindingSeverity::Blocking,
        });
    }
    let kept = apply_policy(
        policy_findings,
        selected,
        &suppressions(config),
        &scoped_ignores(config.rule_ignores.iter()),
    )
    .map_err(RulesFailure::Value)?;
    let mut findings: Vec<Option<RuleFinding>> = findings.into_iter().map(Some).collect();
    Ok(kept
        .into_iter()
        .filter_map(|index| findings[index].take())
        .collect())
}
