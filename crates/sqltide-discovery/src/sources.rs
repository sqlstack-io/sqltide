//! Each pipeline's source, found by following transforms' driving inputs to a declared source.

use std::collections::{BTreeSet, HashMap, HashSet};

/// One pipeline: a display path for errors, and its non-view transforms as
/// `(name, driving input)` in authored order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PipelineTransforms {
    pub directory: String,
    pub transforms: Vec<(String, String)>,
}

/// The source name of each pipeline in order, `None` when it has no transforms, stopping at
/// the first pipeline that fails with its error message. Resolved chains are cached across
/// pipelines.
#[must_use]
pub fn pipeline_sources(
    pipelines: &[PipelineTransforms],
    inputs_by_transform: &HashMap<String, String>,
    sources: &HashSet<String>,
) -> (Vec<Option<String>>, Option<String>) {
    let mut cache: HashMap<String, String> = HashMap::new();
    let mut resolved: Vec<Option<String>> = Vec::with_capacity(pipelines.len());
    for pipeline in pipelines {
        match pipeline_source(pipeline, inputs_by_transform, sources, &mut cache) {
            Ok(source) => resolved.push(source),
            Err(message) => return (resolved, Some(message)),
        }
    }
    (resolved, None)
}

fn pipeline_source(
    pipeline: &PipelineTransforms,
    inputs_by_transform: &HashMap<String, String>,
    sources: &HashSet<String>,
    cache: &mut HashMap<String, String>,
) -> Result<Option<String>, String> {
    let directory: &str = &pipeline.directory;
    let mut found: BTreeSet<String> = BTreeSet::new();
    for (name, input) in &pipeline.transforms {
        let mut path: Vec<&str> = Vec::new();
        let mut visiting: BTreeSet<&str> = BTreeSet::new();
        let (mut current, mut current_input): (&str, &str) = (name, input);
        let source: String = loop {
            if let Some(source) = cache.get(current) {
                break source.clone();
            }
            if !visiting.insert(current) {
                let names: Vec<&str> = visiting.into_iter().collect();
                return Err(format!(
                    "Pipeline directory '{directory}' has a driving-input cycle: {}",
                    names.join(", ")
                ));
            }
            path.push(current);
            if sources.contains(current_input) {
                break current_input.to_owned();
            }
            let Some((next, next_input)) = inputs_by_transform.get_key_value(current_input) else {
                return Err(format!(
                    "Pipeline directory '{directory}' references unknown driving input \
                     '{current_input}'"
                ));
            };
            (current, current_input) = (next, next_input);
        };
        for transform in path {
            cache.insert(transform.to_owned(), source.clone());
        }
        found.insert(source);
    }
    if found.len() > 1 {
        let names: Vec<String> = found.into_iter().collect();
        return Err(format!(
            "Pipeline directory '{directory}' must resolve to exactly one source; found {}",
            names.join(", ")
        ));
    }
    Ok(found.into_iter().next())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pipeline(directory: &str, transforms: &[(&str, &str)]) -> PipelineTransforms {
        PipelineTransforms {
            directory: directory.to_owned(),
            transforms: transforms
                .iter()
                .map(|(name, input)| ((*name).to_owned(), (*input).to_owned()))
                .collect(),
        }
    }

    fn run(pipelines: &[PipelineTransforms]) -> (Vec<Option<String>>, Option<String>) {
        let inputs: HashMap<String, String> = pipelines
            .iter()
            .flat_map(|pipeline| pipeline.transforms.iter().cloned())
            .collect();
        let sources: HashSet<String> = HashSet::from(["orders".to_owned(), "customers".to_owned()]);
        pipeline_sources(pipelines, &inputs, &sources)
    }

    #[test]
    fn follows_chains_across_pipelines() {
        assert_eq!(
            run(&[
                pipeline(
                    "a",
                    &[("orders_clean", "orders"), ("orders_daily", "orders_clean")]
                ),
                pipeline("b", &[("orders_report", "orders_daily")]),
                pipeline("c", &[]),
            ]),
            (
                vec![Some("orders".to_owned()), Some("orders".to_owned()), None],
                None
            )
        );
    }

    #[test]
    fn stops_at_the_first_failing_pipeline() {
        assert_eq!(
            run(&[
                pipeline("a", &[("x", "orders")]),
                pipeline("b", &[("y", "orders"), ("z", "customers")]),
                pipeline("c", &[("w", "missing")]),
            ]),
            (
                vec![Some("orders".to_owned())],
                Some("Pipeline directory 'b' must resolve to exactly one source; found customers, orders".to_owned())
            )
        );
        assert_eq!(
            run(&[pipeline("a", &[("p", "q"), ("q", "p")])]).1,
            Some("Pipeline directory 'a' has a driving-input cycle: p, q".to_owned())
        );
        assert_eq!(
            run(&[pipeline("a", &[("p", "missing")])]).1,
            Some("Pipeline directory 'a' references unknown driving input 'missing'".to_owned())
        );
    }
}
