//! Whole-project discovery validation: unique identities, cross-pipeline allowances, and
//! direct-mode replay policy restrictions.

use std::collections::{HashMap, HashSet};

/// The replay policy facts one authored level defines.
#[derive(Clone, Copy, Debug, Default)]
pub struct ReplayPolicyFacts {
    pub replay_on_change: bool,
    pub bounded_replay_fallback: bool,
}

/// One authored model.
pub struct ModelFacts {
    pub name: String,
    /// Table models carry replay policy; views do not.
    pub is_table: bool,
    pub policy: ReplayPolicyFacts,
}

/// One loaded pipeline.
pub struct PipelineFacts {
    pub name: String,
    pub file_path: String,
    pub is_virtual: bool,
    pub policy: ReplayPolicyFacts,
    pub models: Vec<ModelFacts>,
}

/// The project-level replay policy and where it is authored.
pub struct ProjectPolicyFacts {
    pub file_path: String,
    /// Whether `defaults.pipeline_mode` is direct.
    pub default_mode_is_direct: bool,
    pub policy: ReplayPolicyFacts,
}

/// The loaded project's cross-pipeline allowances.
pub struct ProjectReferenceFacts {
    pub file_path: String,
    /// `(upstream pipeline, downstream pipeline)`.
    pub allowed_cross_pipeline_references: Vec<(String, String)>,
}

/// Every attached project input that identity validation reads.
pub struct ProjectInputFacts {
    /// `(source name, source file path)` in discovery order.
    pub sources: Vec<(String, String)>,
    pub pipelines: Vec<PipelineFacts>,
    pub project_references: Option<ProjectReferenceFacts>,
    pub project_policy: Option<ProjectPolicyFacts>,
    /// `(test name, file path)`.
    pub tests: Vec<(String, String)>,
    /// `(audit name, file path)`.
    pub audits: Vec<(String, String)>,
}

/// Reject duplicate identities and invalid replay policy, in a fixed order: pipeline names,
/// cross-pipeline allowances, replay policies, logical node names, test names, audit names.
///
/// # Errors
///
/// Returns the discovery error message for the first violation.
pub fn validate_project_inputs(input: &ProjectInputFacts) -> Result<(), String> {
    validate_unique_names(
        "pipeline",
        input
            .pipelines
            .iter()
            .map(|pipeline| (pipeline.name.as_str(), pipeline.file_path.as_str())),
    )?;
    if let Some(project) = &input.project_references {
        validate_cross_pipeline_references(project, &input.pipelines)?;
    }
    validate_replay_policies(input.project_policy.as_ref(), &input.pipelines)?;
    validate_logical_node_names(&input.sources, &input.pipelines)?;
    validate_unique_names(
        "SQL test",
        input
            .tests
            .iter()
            .map(|(name, path)| (name.as_str(), path.as_str())),
    )?;
    validate_unique_names(
        "SQL audit",
        input
            .audits
            .iter()
            .map(|(name, path)| (name.as_str(), path.as_str())),
    )
}

/// Reject change-driven replay policy at every authored level in direct mode.
///
/// Project-level policy is allowed only when some pipeline is virtual; pipeline and table
/// model policy only inside virtual pipelines.
///
/// # Errors
///
/// Returns the discovery error message for the first violation.
pub fn validate_replay_policies(
    project: Option<&ProjectPolicyFacts>,
    pipelines: &[PipelineFacts],
) -> Result<(), String> {
    let has_virtual_pipeline: bool = pipelines.iter().any(|pipeline| pipeline.is_virtual);
    if let Some(project) = project.filter(|_| !has_virtual_pipeline) {
        let reason: &str = if project.default_mode_is_direct {
            "defaults.pipeline_mode is direct"
        } else {
            "every pipeline overrides its mode to direct"
        };
        for (defined, key) in [
            (project.policy.replay_on_change, "replay_on_change"),
            (
                project.policy.bounded_replay_fallback,
                "bounded_replay_fallback",
            ),
        ] {
            if defined {
                return Err(format!(
                    "Project file '{}' cannot define defaults.{key} when {reason}",
                    project.file_path
                ));
            }
        }
    }
    for pipeline in pipelines.iter().filter(|pipeline| !pipeline.is_virtual) {
        for (defined, key) in [
            (pipeline.policy.replay_on_change, "replay_on_change"),
            (
                pipeline.policy.bounded_replay_fallback,
                "bounded_replay_fallback",
            ),
        ] {
            if defined {
                return Err(format!(
                    "Pipeline '{}' cannot define {key} when its mode is direct",
                    pipeline.file_path
                ));
            }
        }
        for model in pipeline.models.iter().filter(|model| model.is_table) {
            for (defined, key) in [
                (model.policy.replay_on_change, "replay_on_change"),
                (
                    model.policy.bounded_replay_fallback,
                    "bounded_replay_fallback",
                ),
            ] {
                if defined {
                    return Err(format!(
                        "Model '{}' in '{}' cannot define {key} when its pipeline mode is direct",
                        model.name, pipeline.file_path
                    ));
                }
            }
        }
    }
    Ok(())
}

fn validate_cross_pipeline_references(
    project: &ProjectReferenceFacts,
    pipelines: &[PipelineFacts],
) -> Result<(), String> {
    let known: HashSet<&str> = pipelines
        .iter()
        .map(|pipeline| pipeline.name.as_str())
        .collect();
    for (upstream, downstream) in &project.allowed_cross_pipeline_references {
        let unknown: Vec<&str> = [upstream.as_str(), downstream.as_str()]
            .into_iter()
            .filter(|name| !known.contains(name))
            .collect();
        if !unknown.is_empty() {
            return Err(format!(
                "{} allowed cross-pipeline reference {upstream} -> {downstream} names unknown \
                 pipeline(s): {}",
                project.file_path,
                unknown.join(", ")
            ));
        }
    }
    Ok(())
}

/// Pipeline and model names must not repeat each other or any source name. When two sources
/// share a name, the later source file is the one reported.
fn validate_logical_node_names(
    sources: &[(String, String)],
    pipelines: &[PipelineFacts],
) -> Result<(), String> {
    let mut known: HashMap<&str, &str> = sources
        .iter()
        .map(|(name, path)| (name.as_str(), path.as_str()))
        .collect();
    for pipeline in pipelines {
        if let Some(existing) = known.insert(&pipeline.name, &pipeline.file_path) {
            return Err(format!(
                "Logical resource name '{}' is defined in both '{existing}' and '{}'",
                pipeline.name, pipeline.file_path
            ));
        }
        for model in &pipeline.models {
            if let Some(existing) = known.insert(&model.name, &pipeline.file_path) {
                return Err(format!(
                    "Logical node name '{}' is defined in both '{existing}' and '{}'",
                    model.name, pipeline.file_path
                ));
            }
        }
    }
    Ok(())
}

fn validate_unique_names<'a>(
    resource_kind: &str,
    names_and_paths: impl Iterator<Item = (&'a str, &'a str)>,
) -> Result<(), String> {
    let mut path_by_name: HashMap<&str, &str> = HashMap::new();
    for (name, path) in names_and_paths {
        if let Some(existing) = path_by_name.insert(name, path) {
            return Err(format!(
                "Duplicate {resource_kind} name '{name}' found in '{existing}' and '{path}'"
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pipeline(name: &str, path: &str, models: &[&str]) -> PipelineFacts {
        PipelineFacts {
            name: name.to_owned(),
            file_path: path.to_owned(),
            is_virtual: false,
            policy: ReplayPolicyFacts::default(),
            models: models
                .iter()
                .map(|name| ModelFacts {
                    name: (*name).to_owned(),
                    is_table: true,
                    policy: ReplayPolicyFacts::default(),
                })
                .collect(),
        }
    }

    fn inputs(pipelines: Vec<PipelineFacts>) -> ProjectInputFacts {
        ProjectInputFacts {
            sources: vec![
                ("orders".to_owned(), "sources/a.yml".to_owned()),
                ("orders".to_owned(), "sources/b.yml".to_owned()),
            ],
            pipelines,
            project_references: None,
            project_policy: None,
            tests: Vec::new(),
            audits: Vec::new(),
        }
    }

    #[test]
    fn reports_the_later_duplicate_source_file() {
        assert_eq!(
            validate_project_inputs(&inputs(vec![pipeline("p", "pipelines/p", &["orders"])])),
            Err(
                "Logical node name 'orders' is defined in both 'sources/b.yml' and \
                 'pipelines/p'"
                    .to_owned()
            )
        );
    }

    #[test]
    fn checks_replay_policy_before_logical_names() {
        let mut direct = pipeline("p", "pipelines/p", &["orders"]);
        direct.models[0].policy.bounded_replay_fallback = true;
        assert_eq!(
            validate_project_inputs(&inputs(vec![direct])),
            Err(
                "Model 'orders' in 'pipelines/p' cannot define bounded_replay_fallback when \
                 its pipeline mode is direct"
                    .to_owned()
            )
        );
        let mut view = pipeline("p", "pipelines/p", &["v"]);
        view.models[0].is_table = false;
        view.models[0].policy.replay_on_change = true;
        assert_eq!(validate_replay_policies(None, &[view]), Ok(()));
    }
}
