//! Python bindings for logical dependency graph construction.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PySet, PyTuple};
use sqltide_compile::graph::{
    GraphInput, GraphModel, GraphRef, ModelKind, ProjectGraph, RefType, build_project_graph,
};

use crate::model_sql::chained;

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const GRAPH_MODELS: &str = "sqltide.compiler.graph.models";
const GRAPH_TYPES: &str = "sqltide.compiler.graph.types";
const GRAPH_EXCEPTIONS: &str = "sqltide.compiler.graph.exceptions";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const DIAGNOSTICS_MODELS: &str = "sqltide.diagnostics.models";

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

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

/// The authored step of one table or view model, else `None`.
fn model_step<'py>(model: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let models: Bound<'py, PyModule> = model.py().import(COMPILE_MODELS)?;
    if model.is_instance(&models.getattr("CompiledTableModel")?)? {
        return Ok(Some(model.getattr("transform")?));
    }
    if model.is_instance(&models.getattr("CompiledViewModel")?)? {
        return Ok(Some(model.getattr("view")?));
    }
    Ok(None)
}

fn graph_model(model: &Bound<'_, PyAny>) -> PyResult<GraphModel> {
    let models: Bound<'_, PyModule> = model.py().import(COMPILE_MODELS)?;
    let kind: ModelKind = if model.is_instance(&models.getattr("CompiledTableModel")?)? {
        model_kind(
            "table",
            model.getattr("transform")?.getattr("source")?.extract()?,
        )?
    } else if model.is_instance(&models.getattr("CompiledViewModel")?)? {
        ModelKind::View
    } else {
        ModelKind::Unsupported
    };
    let refs: Vec<GraphRef> = model
        .getattr("parsed_refs")?
        .try_iter()?
        .map(|parsed_ref| {
            let parsed_ref: Bound<'_, PyAny> = parsed_ref?;
            let kind: Bound<'_, PyAny> = parsed_ref.getattr("ref_type")?;
            let kind: Option<String> = if kind.is_none() {
                None
            } else {
                Some(text(&kind)?)
            };
            Ok(GraphRef {
                name: parsed_ref.getattr("name")?.extract()?,
                ref_type: ref_type(kind.as_deref())?,
            })
        })
        .collect::<PyResult<_>>()?;
    Ok(GraphModel {
        name: model.getattr("key")?.getattr("name")?.extract()?,
        pipeline: model.getattr("pipeline_name")?.extract()?,
        kind,
        refs,
    })
}

fn items<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    value.try_iter()?.collect()
}

/// The authored source location of one model reference, when both are known.
fn reference_location<'py>(
    model: &Bound<'py, PyAny>,
    parsed_ref: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = model.py();
    let Some(step) = model_step(model)? else {
        return Ok(py.None().into_bound(py));
    };
    let path: Bound<'py, PyAny> = step.getattr("source_file_path")?;
    let span: Bound<'py, PyAny> = parsed_ref.getattr("span")?;
    if path.is_none() || span.is_none() {
        return Ok(py.None().into_bound(py));
    }
    let source_line: i64 = step.getattr("source_line")?.extract()?;
    let source_column: i64 = step.getattr("source_column")?.extract()?;
    let line: i64 = span.getattr("line")?.extract()?;
    let column: i64 = span.getattr("column")?.extract()?;
    let end_line: i64 = span.getattr("end_line")?.extract()?;
    let end_column: i64 = span.getattr("end_column")?.extract()?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("path", path)?;
    keywords.set_item("line", source_line + line - 1)?;
    keywords.set_item(
        "column",
        if line == 1 {
            source_column + column - 1
        } else {
            column
        },
    )?;
    keywords.set_item("end_line", source_line + end_line - 1)?;
    keywords.set_item(
        "end_column",
        if end_line == 1 {
            source_column + end_column - 1
        } else {
            end_column
        },
    )?;
    py.import(DIAGNOSTICS_MODELS)?
        .getattr("SourceLocation")?
        .call((), Some(&keywords))
}

/// `GraphInputError` raised from `None` over the native graph failure.
fn graph_input_error(
    py: Python<'_>,
    models: &[Bound<'_, PyAny>],
    message: String,
    reference: Option<(usize, usize)>,
) -> PyResult<PyErr> {
    let context: Bound<'_, PyAny> = PyValueError::new_err((message.clone(), reference))
        .value(py)
        .clone()
        .into_any();
    let keywords: Bound<'_, PyDict> = PyDict::new(py);
    if let Some((model_index, reference_index)) = reference {
        let model: &Bound<'_, PyAny> = &models[model_index];
        let parsed_ref: Bound<'_, PyAny> =
            model.getattr("parsed_refs")?.get_item(reference_index)?;
        keywords.set_item("location", reference_location(model, &parsed_ref)?)?;
    }
    let error: Bound<'_, PyAny> = py
        .import(GRAPH_EXCEPTIONS)?
        .getattr("GraphInputError")?
        .call((message,), Some(&keywords))?;
    chained(&error, &context, None)?;
    Ok(PyErr::from_value(error))
}

/// Build, validate, and order one immutable typed logical graph over a compiled project.
#[pyfunction]
#[pyo3(signature = (*, project))]
fn build_project_graph_from_compiled_project<'py>(
    py: Python<'py>,
    project: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let sources: Vec<Bound<'py, PyAny>> = items(&project.getattr("sources")?)?;
    let models: Vec<Bound<'py, PyAny>> = items(&project.getattr("models")?)?;
    let pipeline_modes: Vec<(String, String)> = items(&project.getattr("pipelines")?)?
        .iter()
        .map(|pipeline| {
            let pipeline: Bound<'py, PyAny> = pipeline.getattr("pipeline")?;
            Ok((
                pipeline.getattr("name")?.extract()?,
                text(&pipeline.getattr("mode")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let input: GraphInput = GraphInput {
        sources: sources
            .iter()
            .map(|source| source.getattr("key")?.getattr("name")?.extract())
            .collect::<PyResult<_>>()?,
        models: models.iter().map(graph_model).collect::<PyResult<_>>()?,
        pipeline_modes,
        pipeline_reference_scope: project.getattr("model_reference_scope")?.ne(py
            .import(DISCOVERY_TYPES)?
            .getattr("ModelReferenceScope")?
            .getattr("PROJECT")?)?,
        allowed_cross_pipeline_references: items(
            &project.getattr("allowed_cross_pipeline_references")?,
        )?
        .iter()
        .map(|reference| {
            Ok((
                reference.getattr("upstream_pipeline")?.extract()?,
                reference.getattr("downstream_pipeline")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?,
    };
    let graph: ProjectGraph = match py.detach(|| build_project_graph(&input)) {
        Ok(graph) => graph,
        Err(error) => {
            return Err(graph_input_error(
                py,
                &models,
                error.message,
                error.reference,
            )?);
        }
    };
    let input_keys: Vec<Bound<'py, PyAny>> = sources
        .iter()
        .chain(models.iter())
        .map(|resource| resource.getattr("key"))
        .collect::<PyResult<_>>()?;
    let mut keys: Vec<Option<Bound<'py, PyAny>>> = vec![None; graph.keys.len()];
    for (input_index, &position) in graph.input_keys.iter().enumerate().rev() {
        keys[position] = Some(input_keys[input_index].clone());
    }
    let keys: Vec<Bound<'py, PyAny>> = keys
        .into_iter()
        .map(|key| key.ok_or_else(|| PyValueError::new_err("Graph key has no input")))
        .collect::<PyResult<_>>()?;
    let edge_class: Bound<'py, PyAny> = py.import(GRAPH_MODELS)?.getattr("DependencyEdge")?;
    let edge_types: Bound<'py, PyAny> = py.import(GRAPH_TYPES)?.getattr("DependencyEdgeType")?;
    let mut upstream: Vec<Vec<Bound<'py, PyAny>>> = Vec::with_capacity(keys.len());
    for (key, edges) in keys.iter().zip(&graph.upstream) {
        upstream.push(
            edges
                .iter()
                .map(|edge| {
                    let keywords: Bound<'py, PyDict> = PyDict::new(py);
                    keywords.set_item("upstream_key", &keys[edge.upstream])?;
                    keywords.set_item("downstream_key", key)?;
                    keywords
                        .set_item("edge_type", edge_types.call1((edge.edge_type.as_str(),))?)?;
                    edge_class.call((), Some(&keywords))
                })
                .collect::<PyResult<_>>()?,
        );
    }
    let upstream_by_key: Bound<'py, PyDict> = PyDict::new(py);
    let downstream_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for (position, key) in keys.iter().enumerate() {
        upstream_by_key.set_item(key, PyTuple::new(py, &upstream[position])?)?;
        let downstream: Vec<&Bound<'py, PyAny>> = graph.downstream[position]
            .iter()
            .map(|edge| {
                let index: usize = graph.upstream[edge.downstream]
                    .iter()
                    .position(|candidate| candidate == edge)
                    .unwrap_or_default();
                &upstream[edge.downstream][index]
            })
            .collect();
        downstream_by_key.set_item(key, PyTuple::new(py, downstream)?)?;
    }
    let ordered: Vec<&Bound<'py, PyAny>> = graph
        .ordered
        .iter()
        .map(|&position| &keys[position])
        .collect();
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("project", project)?;
    keywords.set_item("upstream_edges_by_key", upstream_by_key)?;
    keywords.set_item("downstream_edges_by_key", downstream_by_key)?;
    keywords.set_item("ordered_keys", PyTuple::new(py, ordered)?)?;
    py.import(GRAPH_MODELS)?
        .getattr("ProjectGraph")?
        .call((), Some(&keywords))
}

/// Roots and the keys reachable from them through only the requested edge types, in graph order.
#[pyfunction]
#[pyo3(signature = (*, graph, root_keys, direction, edge_types))]
fn collect_reachable_keys<'py>(
    py: Python<'py>,
    graph: &Bound<'py, PyAny>,
    root_keys: &Bound<'py, PyAny>,
    direction: &Bound<'py, PyAny>,
    edge_types: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let upstream: bool = direction.eq(py
        .import(GRAPH_TYPES)?
        .getattr("GraphTraversalDirection")?
        .getattr("UPSTREAM")?)?;
    let (edges_by_key, reachable): (Bound<'py, PyAny>, &str) = if upstream {
        (graph.getattr("upstream_edges_by_key")?, "upstream_key")
    } else {
        (graph.getattr("downstream_edges_by_key")?, "downstream_key")
    };
    let ordered_keys: Vec<Bound<'py, PyAny>> = items(&graph.getattr("ordered_keys")?)?;
    let visited: Bound<'py, PySet> = PySet::new(py, items(root_keys)?)?;
    let mut stack: Vec<Bound<'py, PyAny>> = Vec::new();
    for key in ordered_keys.iter().rev() {
        if root_keys.contains(key)? {
            stack.push(key.clone());
        }
    }
    let empty: Bound<'py, PyTuple> = PyTuple::empty(py);
    while let Some(current) = stack.pop() {
        let edges: Bound<'py, PyAny> = edges_by_key.call_method1("get", (current, &empty))?;
        for edge in items(&edges)?.iter().rev() {
            if !edge_types.contains(edge.getattr("edge_type")?)? {
                continue;
            }
            let key: Bound<'py, PyAny> = edge.getattr(reachable)?;
            if visited.contains(&key)? {
                continue;
            }
            visited.add(&key)?;
            stack.push(key);
        }
    }
    let mut collected: Vec<Bound<'py, PyAny>> = Vec::new();
    for key in ordered_keys {
        if visited.contains(&key)? {
            collected.push(key);
        }
    }
    PyTuple::new(py, collected)
}

fn selector_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import(GRAPH_EXCEPTIONS)?
            .getattr("GraphInputError")?
            .call1((message,))?,
    ))
}

fn pipeline_selector<'py>(
    py: Python<'py>,
    value: &str,
    pipeline_model_keys: &Bound<'py, PyDict>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let Some(keys) = pipeline_model_keys.get_item(value)? else {
        return Err(selector_error(
            py,
            format!("Unknown selected pipeline '{value}'"),
        )?);
    };
    let keys: Vec<Bound<'py, PyAny>> = items(&keys)?;
    if keys.is_empty() {
        return Err(selector_error(
            py,
            format!("Selected pipeline '{value}' does not define any models"),
        )?);
    }
    Ok(keys)
}

/// The model keys of explicitly named models and pipelines, without any graph closure.
#[pyfunction]
#[pyo3(signature = (*, compiled_pipelines, selectors))]
pub(crate) fn resolve_named_model_keys<'py>(
    py: Python<'py>,
    compiled_pipelines: &Bound<'py, PyAny>,
    selectors: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let pipeline_model_keys: Bound<'py, PyDict> = PyDict::new(py);
    let model_key_by_name: Bound<'py, PyDict> = PyDict::new(py);
    for pipeline in items(compiled_pipelines)? {
        let keys: Vec<Bound<'py, PyAny>> = items(&pipeline.getattr("models")?)?
            .iter()
            .map(|model| model.getattr("key"))
            .collect::<PyResult<_>>()?;
        pipeline_model_keys.set_item(
            pipeline.getattr("pipeline")?.getattr("name")?,
            PyTuple::new(py, &keys)?,
        )?;
        for key in keys {
            model_key_by_name.set_item(key.getattr("name")?, key)?;
        }
    }
    let selected: Bound<'py, PySet> = PySet::empty(py)?;
    let frozenset: Bound<'py, PyAny> = py.get_type::<PyFrozenSet>().into_any();
    for raw_selector in items(selectors)? {
        for token in items(&raw_selector.call_method0("split")?)? {
            let token: String = token.extract()?;
            let keys: Vec<Bound<'py, PyAny>> = if token.starts_with('+') || token.ends_with('+') {
                return Err(selector_error(
                    py,
                    format!("Unsupported selector syntax '{token}': '+' is not supported."),
                )?);
            } else if let Some((namespace, value)) = token.split_once(':') {
                match namespace {
                    "pipeline" => pipeline_selector(py, value, &pipeline_model_keys)?,
                    "model" => match model_key_by_name.get_item(value)? {
                        Some(key) => vec![key],
                        None => {
                            return Err(selector_error(
                                py,
                                format!("Unknown selected model '{value}'"),
                            )?);
                        }
                    },
                    _ => {
                        return Err(selector_error(
                            py,
                            format!("Unsupported selector namespace '{namespace}' in '{token}'"),
                        )?);
                    }
                }
            } else if pipeline_model_keys.contains(&token)? {
                pipeline_selector(py, &token, &pipeline_model_keys)?
            } else if let Some(key) = model_key_by_name.get_item(&token)? {
                vec![key]
            } else {
                return Err(selector_error(
                    py,
                    format!("Unknown selected model or pipeline '{token}'"),
                )?);
            };
            let _ =
                selected.call_method1("update", (frozenset.call1((PyTuple::new(py, keys)?,))?,))?;
        }
    }
    frozenset
        .call1((selected,))?
        .cast_into::<PyFrozenSet>()
        .map_err(Into::into)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        build_project_graph_from_compiled_project,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(collect_reachable_keys, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_named_model_keys, module)?)
}
