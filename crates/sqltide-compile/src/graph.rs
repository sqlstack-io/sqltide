//! The typed logical dependency graph over compiled sources and models.
//!
//! Building it derives one typed edge per referenced upstream resource, validates pipeline
//! boundaries and terminal views, and orders every key so each appears after its upstreams.
//! Ties are broken by `(resource type, name)`, so the order is stable across runs.

use std::collections::{BTreeMap, HashMap};

/// Kinds of logical resources, declared in their text order so derived ordering matches it.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum KeyKind {
    Model,
    Source,
}

impl KeyKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Source => "source",
        }
    }
}

/// Semantic relationship of an edge, declared in text order so derived ordering matches it.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum EdgeType {
    DrivingInput,
    MutableReference,
    Reference,
}

impl EdgeType {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DrivingInput => "driving_input",
            Self::MutableReference => "mutable_reference",
            Self::Reference => "reference",
        }
    }
}

/// How a side reference was declared in model SQL.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RefType {
    Reference,
    Mutable,
}

/// What kind of compiled model references come from.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ModelKind {
    /// A table model and the name of its driving source, if any.
    Table {
        driving_source: Option<String>,
    },
    View,
    /// A compiled kind the graph does not support; any reference from it is rejected.
    Unsupported,
}

/// One logical reference parsed from model SQL.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphRef {
    pub name: String,
    pub ref_type: Option<RefType>,
}

/// One compiled model as the graph sees it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphModel {
    pub name: String,
    pub pipeline: String,
    pub kind: ModelKind,
    pub refs: Vec<GraphRef>,
}

/// Everything graph construction reads from a compiled project.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct GraphInput {
    pub sources: Vec<String>,
    pub models: Vec<GraphModel>,
    /// `(pipeline name, mode)`; a later entry for the same pipeline wins.
    pub pipeline_modes: Vec<(String, String)>,
    /// Whether model references must stay inside their pipeline unless explicitly allowed.
    pub pipeline_reference_scope: bool,
    /// `(upstream pipeline, downstream pipeline)` pairs allowed to cross that scope.
    pub allowed_cross_pipeline_references: Vec<(String, String)>,
}

/// One logical resource identity.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct Key {
    pub kind: KeyKind,
    pub name: String,
}

/// One typed edge between two keys, by position in [`ProjectGraph::keys`].
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Edge {
    pub upstream: usize,
    pub downstream: usize,
    pub edge_type: EdgeType,
}

/// A validated graph. `upstream[k]` and `downstream[k]` hold the edges into and out of key `k`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectGraph {
    /// Unique keys: sources, then models, each at its first occurrence.
    pub keys: Vec<Key>,
    /// For each source then model input, its position in `keys`.
    pub input_keys: Vec<usize>,
    pub upstream: Vec<Vec<Edge>>,
    pub downstream: Vec<Vec<Edge>>,
    /// Every key position in dependency order.
    pub ordered: Vec<usize>,
}

/// Why the compiled dependencies do not form a valid graph.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphError {
    pub message: String,
    /// `(model input index, reference index)` of the reference the error is about, if any.
    pub reference: Option<(usize, usize)>,
}

impl GraphError {
    fn new(message: String) -> Self {
        Self {
            message,
            reference: None,
        }
    }
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for GraphError {}

/// Build, validate, and order the logical graph.
///
/// # Errors
///
/// Returns the first [`GraphError`] in this order: an unknown, untyped, unsupported, or
/// conflicting reference; a reference across direct and virtual pipelines; a reference across
/// pipelines the reference scope forbids; a view with downstream models; a dependency cycle.
pub fn build_project_graph(input: &GraphInput) -> Result<ProjectGraph, GraphError> {
    let mut keys: Vec<Key> = Vec::with_capacity(input.sources.len() + input.models.len());
    let mut position_by_key: HashMap<Key, usize> = HashMap::with_capacity(keys.capacity());
    let mut input_keys: Vec<usize> = Vec::with_capacity(keys.capacity());
    let resources = input
        .sources
        .iter()
        .map(|name| (KeyKind::Source, name))
        .chain(
            input
                .models
                .iter()
                .map(|model| (KeyKind::Model, &model.name)),
        );
    for (kind, name) in resources {
        let key: Key = Key {
            kind,
            name: name.clone(),
        };
        let position: usize = *position_by_key.entry(key.clone()).or_insert_with(|| {
            keys.push(key);
            keys.len() - 1
        });
        input_keys.push(position);
    }
    let model_keys: &[usize] = &input_keys[input.sources.len()..];
    let key_by_name: HashMap<&str, usize> = input
        .sources
        .iter()
        .chain(input.models.iter().map(|model| &model.name))
        .map(String::as_str)
        .zip(input_keys.iter().copied())
        .collect();

    let mut upstream: Vec<Vec<Edge>> = vec![Vec::new(); keys.len()];
    for (model, &model_key) in input.models.iter().zip(model_keys) {
        upstream[model_key] = model_upstream_edges(model, model_key, &key_by_name, &keys)?;
    }
    let mut downstream: Vec<Vec<Edge>> = vec![Vec::new(); keys.len()];
    for edge in upstream.iter().flatten() {
        downstream[edge.upstream].push(*edge);
    }
    for edges in &mut downstream {
        edges.sort_by(|left, right| {
            (&keys[left.downstream], left.edge_type)
                .cmp(&(&keys[right.downstream], right.edge_type))
        });
    }

    let model_by_key: HashMap<usize, &GraphModel> =
        model_keys.iter().copied().zip(&input.models).collect();
    validate_pipeline_modes(input, model_keys, &model_by_key, &upstream)?;
    validate_reference_scope(input)?;
    validate_terminal_views(input, model_keys, &keys, &downstream)?;
    let ordered: Vec<usize> = topological_order(&keys, &upstream, &downstream)?;
    Ok(ProjectGraph {
        keys,
        input_keys,
        upstream,
        downstream,
        ordered,
    })
}

fn model_upstream_edges(
    model: &GraphModel,
    model_key: usize,
    key_by_name: &HashMap<&str, usize>,
    keys: &[Key],
) -> Result<Vec<Edge>, GraphError> {
    let mut edge_by_upstream: HashMap<usize, Edge> = HashMap::with_capacity(model.refs.len());
    for reference in &model.refs {
        let Some(&upstream) = key_by_name.get(reference.name.as_str()) else {
            return Err(GraphError::new(format!(
                "Model '{}' references unknown logical resource '{}'",
                model.name, reference.name
            )));
        };
        let edge: Edge = Edge {
            upstream,
            downstream: model_key,
            edge_type: edge_type(model, reference)?,
        };
        if let Some(existing) = edge_by_upstream.insert(upstream, edge)
            && existing.edge_type != edge.edge_type
        {
            return Err(GraphError::new(format!(
                "Model '{}' declares conflicting reference types for '{}'",
                model.name, reference.name
            )));
        }
    }
    let mut edges: Vec<Edge> = edge_by_upstream.into_values().collect();
    edges.sort_by(|left, right| {
        (&keys[left.upstream], left.edge_type).cmp(&(&keys[right.upstream], right.edge_type))
    });
    Ok(edges)
}

fn edge_type(model: &GraphModel, reference: &GraphRef) -> Result<EdgeType, GraphError> {
    let driving_source: Option<&str> = match &model.kind {
        ModelKind::View => return Ok(EdgeType::Reference),
        ModelKind::Unsupported => {
            return Err(GraphError::new(format!(
                "Model '{}' has an unsupported compiled kind",
                model.name
            )));
        }
        ModelKind::Table { driving_source } => driving_source.as_deref(),
    };
    if driving_source == Some(reference.name.as_str()) {
        return Ok(EdgeType::DrivingInput);
    }
    match reference.ref_type {
        Some(RefType::Reference) => Ok(EdgeType::Reference),
        Some(RefType::Mutable) => Ok(EdgeType::MutableReference),
        None => Err(GraphError::new(format!(
            "Model '{}' has an untyped side reference to '{}'",
            model.name, reference.name
        ))),
    }
}

fn validate_pipeline_modes(
    input: &GraphInput,
    model_keys: &[usize],
    model_by_key: &HashMap<usize, &GraphModel>,
    upstream: &[Vec<Edge>],
) -> Result<(), GraphError> {
    let mode_by_pipeline: HashMap<&str, &str> = input
        .pipeline_modes
        .iter()
        .map(|(pipeline, mode)| (pipeline.as_str(), mode.as_str()))
        .collect();
    for (downstream_model, &model_key) in input.models.iter().zip(model_keys) {
        for edge in &upstream[model_key] {
            let Some(upstream_model) = model_by_key.get(&edge.upstream) else {
                continue;
            };
            let (Some(upstream_mode), Some(downstream_mode)) = (
                mode_by_pipeline.get(upstream_model.pipeline.as_str()),
                mode_by_pipeline.get(downstream_model.pipeline.as_str()),
            ) else {
                continue;
            };
            if upstream_mode == downstream_mode {
                continue;
            }
            return Err(GraphError::new(format!(
                "Pipeline '{}' is {downstream_mode} but model '{}' references model '{}' in \
                 {upstream_mode} pipeline '{}'. Relations between direct and virtual pipelines \
                 are not allowed.",
                downstream_model.pipeline,
                downstream_model.name,
                upstream_model.name,
                upstream_model.pipeline
            )));
        }
    }
    Ok(())
}

fn validate_reference_scope(input: &GraphInput) -> Result<(), GraphError> {
    if !input.pipeline_reference_scope {
        return Ok(());
    }
    let model_by_name: HashMap<&str, &GraphModel> = input
        .models
        .iter()
        .map(|model| (model.name.as_str(), model))
        .collect();
    for (model_index, downstream_model) in input.models.iter().enumerate() {
        for (reference_index, reference) in downstream_model.refs.iter().enumerate() {
            let Some(upstream_model) = model_by_name.get(reference.name.as_str()) else {
                continue;
            };
            if upstream_model.pipeline == downstream_model.pipeline
                || input.allowed_cross_pipeline_references.iter().any(
                    |(upstream_pipeline, downstream_pipeline)| {
                        *upstream_pipeline == upstream_model.pipeline
                            && *downstream_pipeline == downstream_model.pipeline
                    },
                )
            {
                continue;
            }
            return Err(GraphError {
                message: format!(
                    "Model '{}' in pipeline '{}' references model '{}' in pipeline '{}', but \
                     dependencies.model_reference_scope is 'pipeline' and no directional \
                     allowed_cross_pipeline_references entry permits this edge.",
                    downstream_model.name,
                    downstream_model.pipeline,
                    upstream_model.name,
                    upstream_model.pipeline
                ),
                reference: Some((model_index, reference_index)),
            });
        }
    }
    Ok(())
}

fn validate_terminal_views(
    input: &GraphInput,
    model_keys: &[usize],
    keys: &[Key],
    downstream: &[Vec<Edge>],
) -> Result<(), GraphError> {
    for (model, &model_key) in input.models.iter().zip(model_keys) {
        if model.kind != ModelKind::View || downstream[model_key].is_empty() {
            continue;
        }
        let names: Vec<&str> = downstream[model_key]
            .iter()
            .map(|edge| keys[edge.downstream].name.as_str())
            .collect();
        return Err(GraphError::new(format!(
            "View model '{}' must be terminal; referenced by downstream model(s): {}",
            model.name,
            names.join(", ")
        )));
    }
    Ok(())
}

fn topological_order(
    keys: &[Key],
    upstream: &[Vec<Edge>],
    downstream: &[Vec<Edge>],
) -> Result<Vec<usize>, GraphError> {
    let mut indegree: Vec<usize> = upstream.iter().map(Vec::len).collect();
    let mut ready: BTreeMap<&Key, usize> = indegree
        .iter()
        .enumerate()
        .filter(|(_, degree)| **degree == 0)
        .map(|(position, _)| (&keys[position], position))
        .collect();
    let mut ordered: Vec<usize> = Vec::with_capacity(keys.len());
    while let Some((_, current)) = ready.pop_first() {
        ordered.push(current);
        for edge in &downstream[current] {
            indegree[edge.downstream] -= 1;
            if indegree[edge.downstream] == 0 {
                ready.insert(&keys[edge.downstream], edge.downstream);
            }
        }
    }
    if ordered.len() == keys.len() {
        return Ok(ordered);
    }
    let mut unresolved: Vec<&Key> = indegree
        .iter()
        .enumerate()
        .filter(|(_, degree)| **degree > 0)
        .map(|(position, _)| &keys[position])
        .collect();
    unresolved.sort();
    let names: Vec<String> = unresolved
        .iter()
        .map(|key| format!("{}:{}", key.kind.as_str(), key.name))
        .collect();
    Err(GraphError::new(format!(
        "Dependency cycle detected involving: {}",
        names.join(", ")
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(
        name: &str,
        pipeline: &str,
        source: Option<&str>,
        refs: &[(&str, Option<RefType>)],
    ) -> GraphModel {
        GraphModel {
            name: name.to_owned(),
            pipeline: pipeline.to_owned(),
            kind: ModelKind::Table {
                driving_source: source.map(str::to_owned),
            },
            refs: refs
                .iter()
                .map(|(name, ref_type)| GraphRef {
                    name: (*name).to_owned(),
                    ref_type: *ref_type,
                })
                .collect(),
        }
    }

    fn names(graph: &ProjectGraph, positions: &[usize]) -> Vec<String> {
        positions
            .iter()
            .map(|&position| {
                let key: &Key = &graph.keys[position];
                format!("{}:{}", key.kind.as_str(), key.name)
            })
            .collect()
    }

    #[test]
    fn orders_upstreams_first_and_breaks_ties_by_type_then_name() {
        let input: GraphInput = GraphInput {
            sources: vec!["orders".to_owned(), "a_events".to_owned()],
            models: vec![
                table(
                    "z_enriched",
                    "p",
                    Some("orders"),
                    &[("orders", None), ("b_dim", Some(RefType::Reference))],
                ),
                table("b_dim", "p", Some("a_events"), &[("a_events", None)]),
            ],
            ..GraphInput::default()
        };
        let graph: ProjectGraph = build_project_graph(&input).unwrap();
        // "model" sorts before "source", so ready models come first, but b_dim waits for a_events.
        assert_eq!(
            names(&graph, &graph.ordered),
            [
                "source:a_events",
                "model:b_dim",
                "source:orders",
                "model:z_enriched"
            ]
        );
        let enriched: usize = graph.input_keys[2];
        let upstream: Vec<(String, EdgeType)> = graph.upstream[enriched]
            .iter()
            .map(|edge| (graph.keys[edge.upstream].name.clone(), edge.edge_type))
            .collect();
        assert_eq!(
            upstream,
            [
                ("b_dim".to_owned(), EdgeType::Reference),
                ("orders".to_owned(), EdgeType::DrivingInput)
            ]
        );
    }

    #[test]
    fn reports_every_key_on_a_cycle() {
        let input: GraphInput = GraphInput {
            models: vec![
                table("b", "p", None, &[("a", Some(RefType::Mutable))]),
                table("a", "p", None, &[("b", Some(RefType::Reference))]),
            ],
            ..GraphInput::default()
        };
        assert_eq!(
            build_project_graph(&input).unwrap_err().message,
            "Dependency cycle detected involving: model:a, model:b"
        );
    }

    #[test]
    fn rejects_conflicting_reference_types_for_one_upstream() {
        let input: GraphInput = GraphInput {
            sources: vec!["s".to_owned()],
            models: vec![
                table("dim", "p", Some("s"), &[("s", None)]),
                table(
                    "fact",
                    "p",
                    None,
                    &[
                        ("dim", Some(RefType::Reference)),
                        ("dim", Some(RefType::Mutable)),
                    ],
                ),
            ],
            ..GraphInput::default()
        };
        assert_eq!(
            build_project_graph(&input).unwrap_err().message,
            "Model 'fact' declares conflicting reference types for 'dim'"
        );
    }

    #[test]
    fn locates_a_forbidden_cross_pipeline_reference() {
        let input: GraphInput = GraphInput {
            sources: vec!["s".to_owned()],
            models: vec![
                table("dim", "one", Some("s"), &[("s", None)]),
                table(
                    "fact",
                    "two",
                    Some("s"),
                    &[("s", None), ("dim", Some(RefType::Reference))],
                ),
            ],
            pipeline_reference_scope: true,
            ..GraphInput::default()
        };
        assert_eq!(
            build_project_graph(&input).unwrap_err().reference,
            Some((1, 1))
        );
        let allowed: GraphInput = GraphInput {
            allowed_cross_pipeline_references: vec![("one".to_owned(), "two".to_owned())],
            ..input
        };
        assert!(build_project_graph(&allowed).is_ok());
    }
}
