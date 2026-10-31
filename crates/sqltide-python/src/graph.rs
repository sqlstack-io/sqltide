//! Python bindings for logical dependency graph construction.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_compile::graph::{
    GraphInput, GraphModel, GraphRef, ModelKind, ProjectGraph, RefType, build_project_graph,
};

/// `(name, pipeline, kind, driving source, [(reference name, reference type)])`.
type ModelArgument = (
    String,
    String,
    String,
    Option<String>,
    Vec<(String, Option<String>)>,
);

/// For each key position, the index of the first source-then-model input with that key.
type KeyInputs = Vec<usize>;
/// For each key, its upstream edges as `(upstream key position, edge type)`.
type UpstreamEdges = Vec<Vec<(usize, &'static str)>>;
/// For each key, its downstream edges as `(downstream key position, index of the same edge in
/// that key's upstream edges)`.
type DownstreamEdges = Vec<Vec<(usize, usize)>>;

fn model_kind(kind: &str, driving_source: Option<String>) -> PyResult<ModelKind> {
    match kind {
        "table" => Ok(ModelKind::Table { driving_source }),
        "view" => Ok(ModelKind::View),
        "unsupported" => Ok(ModelKind::Unsupported),
        other => Err(PyValueError::new_err(format!(
            "Unknown model kind '{other}'"
        ))),
    }
}

fn ref_type(ref_type: Option<&str>) -> PyResult<Option<RefType>> {
    match ref_type {
        None => Ok(None),
        Some("reference") => Ok(Some(RefType::Reference)),
        Some("mutable") => Ok(Some(RefType::Mutable)),
        Some(other) => Err(PyValueError::new_err(format!(
            "Unknown reference type '{other}'"
        ))),
    }
}

/// Build, validate, and order one logical graph.
///
/// Returns `(key inputs, upstream edges, downstream edges, ordered key positions)`. An invalid
/// graph raises `ValueError(message, (model index, reference index) | None)`.
#[pyfunction]
#[pyo3(name = "build_project_graph")]
fn build_project_graph_py(
    py: Python<'_>,
    sources: Vec<String>,
    models: Vec<ModelArgument>,
    pipeline_modes: Vec<(String, String)>,
    pipeline_reference_scope: bool,
    allowed_cross_pipeline_references: Vec<(String, String)>,
) -> PyResult<(KeyInputs, UpstreamEdges, DownstreamEdges, Vec<usize>)> {
    let models: Vec<GraphModel> = models
        .into_iter()
        .map(|(name, pipeline, kind, driving_source, refs)| {
            Ok(GraphModel {
                name,
                pipeline,
                kind: model_kind(&kind, driving_source)?,
                refs: refs
                    .into_iter()
                    .map(|(name, kind)| {
                        Ok(GraphRef {
                            name,
                            ref_type: ref_type(kind.as_deref())?,
                        })
                    })
                    .collect::<PyResult<_>>()?,
            })
        })
        .collect::<PyResult<_>>()?;
    let input: GraphInput = GraphInput {
        sources,
        models,
        pipeline_modes,
        pipeline_reference_scope,
        allowed_cross_pipeline_references,
    };
    let graph: ProjectGraph = py
        .detach(|| build_project_graph(&input))
        .map_err(|error| PyValueError::new_err((error.message, error.reference)))?;
    let mut key_inputs: KeyInputs = vec![usize::MAX; graph.keys.len()];
    for (input_index, &position) in graph.input_keys.iter().enumerate().rev() {
        key_inputs[position] = input_index;
    }
    let upstream: UpstreamEdges = graph
        .upstream
        .iter()
        .map(|edges| {
            edges
                .iter()
                .map(|edge| (edge.upstream, edge.edge_type.as_str()))
                .collect()
        })
        .collect();
    let downstream: DownstreamEdges = graph
        .downstream
        .iter()
        .map(|edges| {
            edges
                .iter()
                .map(|edge| {
                    let index: usize = graph.upstream[edge.downstream]
                        .iter()
                        .position(|candidate| candidate == edge)
                        .unwrap_or_default();
                    (edge.downstream, index)
                })
                .collect()
        })
        .collect();
    Ok((key_inputs, upstream, downstream, graph.ordered))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_project_graph_py, module)?)
}
