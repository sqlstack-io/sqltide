//! SQL audit target validation and effective audit policy resolution.

use std::collections::{BTreeSet, HashMap, HashSet};

pub const DEFAULT_AUDIT_SEVERITY: &str = "error";
pub const WARNING_AUDIT_SEVERITY: &str = "warning";

/// Optional project- or pipeline-level audit policy defaults.
#[derive(Clone, Debug, Default)]
pub struct AuditDefaults {
    pub severity: Option<String>,
    pub cadence_seconds: Option<i128>,
    pub warmup_seconds: Option<i128>,
}

/// A compiled pipeline's audit defaults and the models it owns.
pub struct PipelineAuditScope {
    pub defaults: AuditDefaults,
    pub model_names: Vec<String>,
}

/// One discovered audit's policy inputs.
pub struct AuditPolicyInput {
    pub file_path: String,
    /// The audit name, or the file stem when the audit has no (or an empty) name.
    pub label: String,
    pub has_name: bool,
    pub is_standalone: bool,
    pub referenced_model_names: Vec<String>,
    pub severity: String,
    pub severity_is_explicit: bool,
    pub cadence_seconds_override: Option<i128>,
    pub warmup_seconds_override: Option<i128>,
    pub scheduled_override: Option<bool>,
}

/// One audit's effective policy.
#[derive(Debug, PartialEq, Eq)]
pub struct AuditPolicy {
    pub severity: String,
    pub cadence_seconds: Option<i128>,
    pub warmup_seconds: i128,
    pub scheduled: bool,
}

/// Validate every audit's targets, then resolve each audit's effective policy.
///
/// A pipeline's defaults fall back to the project's; an audit inherits the defaults of every
/// model it references unless it overrides them. When several pipelines own one model name,
/// the last pipeline's defaults apply.
///
/// # Errors
///
/// Returns the compile error message for the first audit that references an unknown model,
/// inherits conflicting cadences, is scheduled without a cadence, or is a scheduled
/// standalone audit without a name.
pub fn resolve_audit_policies(
    audits: &[AuditPolicyInput],
    pipelines: &[PipelineAuditScope],
    project: &AuditDefaults,
) -> Result<Vec<AuditPolicy>, String> {
    let known: HashSet<&str> = pipelines
        .iter()
        .flat_map(|pipeline| pipeline.model_names.iter().map(String::as_str))
        .collect();
    for audit in audits {
        let unknown: Vec<&str> = audit
            .referenced_model_names
            .iter()
            .map(String::as_str)
            .filter(|name| !known.contains(name))
            .collect();
        if !unknown.is_empty() {
            return Err(format!(
                "SQL audit '{}' references unknown models: {}",
                audit.file_path,
                unknown.join(", ")
            ));
        }
    }
    let mut defaults_by_model: HashMap<&str, AuditDefaults> = HashMap::new();
    for pipeline in pipelines {
        let defaults: AuditDefaults = effective_defaults(project, &pipeline.defaults);
        for model_name in &pipeline.model_names {
            defaults_by_model.insert(model_name, defaults.clone());
        }
    }
    audits
        .iter()
        .map(|audit| resolve_audit_policy(audit, &defaults_by_model))
        .collect()
}

/// Non-empty `Some` severity, matching Python truthiness.
fn present(severity: Option<&String>) -> Option<&String> {
    severity.filter(|severity| !severity.is_empty())
}

fn effective_defaults(project: &AuditDefaults, pipeline: &AuditDefaults) -> AuditDefaults {
    AuditDefaults {
        severity: Some(
            present(pipeline.severity.as_ref())
                .or(present(project.severity.as_ref()))
                .map_or_else(|| DEFAULT_AUDIT_SEVERITY.to_owned(), Clone::clone),
        ),
        cadence_seconds: pipeline.cadence_seconds.or(project.cadence_seconds),
        warmup_seconds: Some(
            pipeline
                .warmup_seconds
                .unwrap_or(project.warmup_seconds.unwrap_or(0)),
        ),
    }
}

fn resolve_audit_policy(
    audit: &AuditPolicyInput,
    defaults_by_model: &HashMap<&str, AuditDefaults>,
) -> Result<AuditPolicy, String> {
    // Every referenced model was validated as known, and every known model has defaults.
    let inherited: Vec<&AuditDefaults> = audit
        .referenced_model_names
        .iter()
        .filter_map(|name| defaults_by_model.get(name.as_str()))
        .collect();
    let severity: String = if audit.severity_is_explicit {
        audit.severity.clone()
    } else if inherited
        .iter()
        .any(|item| item.severity.as_deref() == Some(DEFAULT_AUDIT_SEVERITY))
    {
        DEFAULT_AUDIT_SEVERITY.to_owned()
    } else {
        WARNING_AUDIT_SEVERITY.to_owned()
    };
    let warmup_seconds: i128 = audit.warmup_seconds_override.unwrap_or_else(|| {
        inherited
            .iter()
            .map(|item| item.warmup_seconds.unwrap_or(0))
            .max()
            .unwrap_or(0)
    });
    let inherited_cadences: BTreeSet<i128> = inherited
        .iter()
        .filter_map(|item| item.cadence_seconds)
        .collect();
    if audit.cadence_seconds_override.is_none() && inherited_cadences.len() > 1 {
        return Err(format!(
            "SQL audit '{}' inherits conflicting pipeline cadences; define every on the audit",
            audit.label
        ));
    }
    let mut cadence_seconds: Option<i128> = audit
        .cadence_seconds_override
        .or_else(|| inherited_cadences.first().copied());
    if audit.scheduled_override == Some(false) {
        cadence_seconds = None;
    }
    let scheduled: bool = cadence_seconds.is_some();
    if audit.scheduled_override == Some(true) && cadence_seconds.is_none() {
        return Err(format!(
            "SQL audit '{}' sets scheduled: true without an effective cadence",
            audit.label
        ));
    }
    if scheduled && audit.is_standalone && !audit.has_name {
        return Err(format!(
            "Scheduled SQL audit '{}' must define an explicit stable name",
            audit.file_path
        ));
    }
    Ok(AuditPolicy {
        severity,
        cadence_seconds,
        warmup_seconds,
        scheduled,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn audit(models: &[&str]) -> AuditPolicyInput {
        AuditPolicyInput {
            file_path: "audits/a.sql".to_owned(),
            label: "a".to_owned(),
            has_name: false,
            is_standalone: true,
            referenced_model_names: models.iter().map(|name| (*name).to_owned()).collect(),
            severity: DEFAULT_AUDIT_SEVERITY.to_owned(),
            severity_is_explicit: false,
            cadence_seconds_override: None,
            warmup_seconds_override: None,
            scheduled_override: None,
        }
    }

    fn pipeline(
        severity: &str,
        cadence: Option<i128>,
        warmup: i128,
        models: &[&str],
    ) -> PipelineAuditScope {
        PipelineAuditScope {
            defaults: AuditDefaults {
                severity: Some(severity.to_owned()),
                cadence_seconds: cadence,
                warmup_seconds: Some(warmup),
            },
            model_names: models.iter().map(|name| (*name).to_owned()).collect(),
        }
    }

    #[test]
    fn inherits_strictest_severity_and_longest_warmup() {
        let pipelines = [
            pipeline("warning", None, 30, &["a"]),
            pipeline("", None, 10, &["b"]),
        ];
        let project = AuditDefaults {
            severity: Some("warning".to_owned()),
            ..AuditDefaults::default()
        };
        assert_eq!(
            resolve_audit_policies(&[audit(&["a", "b"])], &pipelines, &project),
            Ok(vec![AuditPolicy {
                severity: "warning".to_owned(),
                cadence_seconds: None,
                warmup_seconds: 30,
                scheduled: false,
            }])
        );
    }

    #[test]
    fn rejects_unknown_models_before_policy_errors() {
        let pipelines = [
            pipeline("error", Some(60), 0, &["a"]),
            pipeline("error", Some(120), 0, &["b"]),
        ];
        let audits = [audit(&["a", "b"]), audit(&["c"])];
        assert_eq!(
            resolve_audit_policies(&audits, &pipelines, &AuditDefaults::default()),
            Err("SQL audit 'audits/a.sql' references unknown models: c".to_owned())
        );
        assert_eq!(
            resolve_audit_policies(&audits[..1], &pipelines, &AuditDefaults::default()),
            Err(
                "SQL audit 'a' inherits conflicting pipeline cadences; define every on the audit"
                    .to_owned()
            )
        );
        let scheduled_unnamed = AuditPolicyInput {
            cadence_seconds_override: Some(60),
            ..audit(&["a", "b"])
        };
        assert_eq!(
            resolve_audit_policies(&[scheduled_unnamed], &pipelines, &AuditDefaults::default()),
            Err(
                "Scheduled SQL audit 'audits/a.sql' must define an explicit stable name".to_owned()
            )
        );
    }
}
