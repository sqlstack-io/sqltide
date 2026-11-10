//! The compiled project's logical DAG artifact and its publication manifest.

use std::collections::{BTreeSet, HashMap, HashSet};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use sqltide_compile::manifest::{
    ManifestLogicalResource, ManifestResourceKind, ManifestTarget, manifest_contents,
};

use crate::artifacts::json_dumps;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
/// Persisted manifest contract version, checked again before destruction planning.
const MANIFEST_VERSION: i64 = 1;
/// `datetime.strftime` format of a manifest's default publication time.
const PUBLISHED_AT_FORMAT: &str = "%Y-%m-%d %H:%M:%S.%f";

fn items<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    value.try_iter()?.collect()
}

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

/// `type:name` of one logical resource key.
fn key_id(key: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(format!(
        "{}:{}",
        text(&key.getattr("resource_type")?)?,
        key.getattr("name")?.extract::<String>()?
    ))
}

/// A test, test case or audit's name, else its file stem.
fn identity(resource: &Bound<'_, PyAny>) -> PyResult<String> {
    let name: Bound<'_, PyAny> = resource.getattr("name")?;
    if name.is_truthy()? {
        name.extract()
    } else {
        resource.getattr("file_path")?.getattr("stem")?.extract()
    }
}

fn dag_node<'py>(py: Python<'py>, kind: &str, name: &str) -> PyResult<Bound<'py, PyDict>> {
    let node: Bound<'py, PyDict> = PyDict::new(py);
    node.set_item("id", format!("{kind}:{name}"))?;
    node.set_item("name", name)?;
    node.set_item("type", kind)?;
    Ok(node)
}

/// Deterministic JSON, with a trailing newline, for one compiled project's logical DAG of
/// sources, models, tests and audits.
#[pyfunction]
#[pyo3(signature = (*, graph, macro_registry))]
fn build_dag_json(
    py: Python<'_>,
    graph: &Bound<'_, PyAny>,
    macro_registry: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let project: Bound<'_, PyAny> = graph.getattr("project")?;
    let mut nodes: Vec<Bound<'_, PyDict>> = Vec::new();
    for source in items(&project.getattr("sources")?)? {
        let name: String = source.getattr("key")?.getattr("name")?.extract()?;
        nodes.push(dag_node(py, "source", &name)?);
    }
    let models: Vec<Bound<'_, PyAny>> = items(&project.getattr("models")?)?;
    let mut model_names: HashSet<String> = HashSet::new();
    for model in &models {
        let name: String = model.getattr("key")?.getattr("name")?.extract()?;
        nodes.push(dag_node(py, "model", &name)?);
        let _ = model_names.insert(name);
    }
    let tests: Vec<Bound<'_, PyAny>> = items(&project.getattr("tests")?)?;
    let mut test_names: Vec<String> = tests.iter().map(identity).collect::<PyResult<_>>()?;
    let audits: Vec<Bound<'_, PyAny>> = items(&project.getattr("audits")?)?;
    let mut audit_names: Vec<String> = audits.iter().map(identity).collect::<PyResult<_>>()?;
    let mut sorted_tests: Vec<String> = test_names.clone();
    sorted_tests.sort();
    for name in &sorted_tests {
        nodes.push(dag_node(py, "test", name)?);
    }
    let mut sorted_audits: Vec<String> = audit_names.clone();
    sorted_audits.sort();
    for name in &sorted_audits {
        nodes.push(dag_node(py, "audit", name)?);
    }
    let mut edges: BTreeSet<(String, String, String)> = BTreeSet::new();
    for dependency_edges in items(
        &graph
            .getattr("downstream_edges_by_key")?
            .call_method0("values")?,
    )? {
        for edge in items(&dependency_edges)? {
            let _ = edges.insert((
                key_id(&edge.getattr("upstream_key")?)?,
                key_id(&edge.getattr("downstream_key")?)?,
                text(&edge.getattr("edge_type")?)?,
            ));
        }
    }
    let mut targets_by_test: HashMap<String, Vec<String>> = HashMap::new();
    for test_case in items(&project.getattr("test_cases")?)? {
        let mut targets: Vec<String> = Vec::new();
        for target in items(&test_case.getattr("target_cases")?)? {
            targets.push(target.getattr("target_model_name")?.extract()?);
        }
        let _ = targets_by_test.insert(identity(&test_case)?, targets);
    }
    for test_name in test_names.drain(..) {
        let Some(targets) = targets_by_test.get(&test_name) else {
            continue;
        };
        for target in targets {
            if model_names.contains(target) {
                let _ = edges.insert((
                    format!("model:{target}"),
                    format!("test:{test_name}"),
                    "test".to_owned(),
                ));
            }
        }
    }
    for (audit, audit_name) in audits.iter().zip(audit_names.drain(..)) {
        for model_name in items(&audit.getattr("referenced_model_names")?)? {
            let _ = edges.insert((
                format!("model:{}", model_name.extract::<String>()?),
                format!("audit:{audit_name}"),
                "audit".to_owned(),
            ));
        }
    }
    let edges: Vec<Bound<'_, PyDict>> = edges
        .into_iter()
        .map(|(from_id, to_id, edge_type)| {
            let edge: Bound<'_, PyDict> = PyDict::new(py);
            edge.set_item("edge_type", edge_type)?;
            edge.set_item("from_id", from_id)?;
            edge.set_item("to_id", to_id)?;
            Ok(edge)
        })
        .collect::<PyResult<_>>()?;
    let mut macro_names: Vec<String> = items(&macro_registry.getattr("macros")?)?
        .iter()
        .map(|name| name.extract())
        .collect::<PyResult<_>>()?;
    macro_names.sort();
    let metadata: Bound<'_, PyDict> = PyDict::new(py);
    metadata.set_item("dag_version", 1)?;
    metadata.set_item("tool", "sqltide")?;
    let payload: Bound<'_, PyDict> = PyDict::new(py);
    payload.set_item("edges", PyList::new(py, edges)?)?;
    payload.set_item("metadata", metadata)?;
    payload.set_item("macros", PyList::new(py, macro_names)?)?;
    payload.set_item("nodes", PyList::new(py, nodes)?)?;
    Ok(json_dumps(payload.as_any(), Some(2), true)? + "\n")
}

/// The declared project name, else the project directory name.
#[pyfunction]
#[pyo3(signature = (*, analysis))]
fn resolve_manifest_project_identity<'py>(
    analysis: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let name: Bound<'py, PyAny> = analysis
        .getattr("compiled_project")?
        .getattr("project_name")?;
    if name.is_truthy()? {
        Ok(name)
    } else {
        analysis
            .getattr("discovered_inputs")?
            .getattr("project_dir")?
            .getattr("name")
    }
}

fn resource_kind(resource: &Bound<'_, PyAny>) -> PyResult<ManifestResourceKind> {
    let models: Bound<'_, PyModule> = resource.py().import(ADAPTER_MODELS)?;
    Ok(
        if resource.is_instance(&models.getattr("AdapterManagedSource")?)? {
            ManifestResourceKind::ManagedSource
        } else if resource.is_instance(&models.getattr("AdapterTable")?)? {
            ManifestResourceKind::Table
        } else if resource.is_instance(&models.getattr("AdapterView")?)? {
            ManifestResourceKind::View
        } else {
            ManifestResourceKind::MaterializedView
        },
    )
}

/// Every key downstream of `root` through any dependency edge.
fn downstream_closure<'py>(
    graph: &Bound<'py, PyAny>,
    root: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let edges_by_key: Bound<'py, PyAny> = graph.getattr("downstream_edges_by_key")?;
    let visited: Bound<'py, PyDict> = PyDict::new(graph.py());
    let mut stack: Vec<Bound<'py, PyAny>> = vec![root.clone()];
    let mut reached: Vec<Bound<'py, PyAny>> = Vec::new();
    while let Some(key) = stack.pop() {
        let edges: Bound<'py, PyAny> =
            edges_by_key.call_method1("get", (&key, PyTuple::empty(graph.py())))?;
        for edge in items(&edges)? {
            let downstream: Bound<'py, PyAny> = edge.getattr("downstream_key")?;
            if downstream.eq(root)? || visited.contains(&downstream)? {
                continue;
            }
            visited.set_item(&downstream, true)?;
            reached.push(downstream.clone());
            stack.push(downstream);
        }
    }
    Ok(reached)
}

/// The sorted names of the pipelines that own or consume each logical resource: its own
/// pipeline, and for a source every pipeline with a model downstream of it.
fn pipeline_names_by_key<'py>(
    analysis: &Bound<'py, PyAny>,
    project: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = analysis.py();
    let names: Bound<'py, PyDict> = PyDict::new(py);
    let add = |key: Bound<'py, PyAny>, name: Bound<'py, PyAny>| -> PyResult<()> {
        let existing: Bound<'py, PyAny> = names
            .as_any()
            .call_method1("setdefault", (key, pyo3::types::PySet::empty(py)?))?;
        existing.call_method1("add", (name,)).map(|_| ())
    };
    for pipeline in items(&project.getattr("pipelines")?)? {
        let name: Bound<'py, PyAny> = pipeline.getattr("pipeline")?.getattr("name")?;
        let source: Bound<'py, PyAny> = pipeline.getattr("source")?;
        if !source.is_none() {
            add(source.getattr("key")?, name.clone())?;
        }
        for model in items(&pipeline.getattr("models")?)? {
            add(model.getattr("key")?, name.clone())?;
        }
    }
    let pipeline_by_model: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(&project.getattr("models")?)? {
        pipeline_by_model.set_item(model.getattr("key")?, model.getattr("pipeline_name")?)?;
    }
    let graph: Bound<'py, PyAny> = analysis.getattr("graph")?;
    for source in items(&project.getattr("sources")?)? {
        let key: Bound<'py, PyAny> = source.getattr("key")?;
        let existing: Bound<'py, PyAny> = names
            .as_any()
            .call_method1("setdefault", (&key, pyo3::types::PySet::empty(py)?))?;
        for downstream in downstream_closure(&graph, &key)? {
            if let Some(name) = pipeline_by_model.get_item(&downstream)? {
                let _ = existing.call_method1("add", (name,))?;
            }
        }
    }
    Ok(names)
}

/// One complete, deterministically fingerprinted project manifest.
#[pyfunction]
#[pyo3(signature = (
    *, analysis, invocation_id, project_identity, target_database, tool_version,
    project_revision = None, published_at = None
))]
#[allow(clippy::too_many_arguments)]
fn build_manifest<'py>(
    py: Python<'py>,
    analysis: &Bound<'py, PyAny>,
    invocation_id: &Bound<'py, PyAny>,
    project_identity: &str,
    target_database: &str,
    tool_version: &Bound<'py, PyAny>,
    project_revision: Option<Bound<'py, PyAny>>,
    published_at: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let realized: Bound<'py, PyAny> = analysis.getattr("realized_project")?;
    let project: Bound<'py, PyAny> = realized.getattr("project")?;
    let target_name: Bound<'py, PyAny> = project.getattr("target_name")?;
    let target_name: String = if target_name.is_truthy()? {
        target_name.extract()?
    } else {
        String::new()
    };
    let is_production: bool = project.getattr("production_target")?.extract()?;
    let pipeline_names: Vec<String> = items(&project.getattr("pipelines")?)?
        .iter()
        .map(|pipeline| pipeline.getattr("pipeline")?.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    let names_by_key: Bound<'py, PyDict> = pipeline_names_by_key(analysis, &project)?;
    let mut logical_resources: Vec<ManifestLogicalResource> = Vec::new();
    for entry in items(
        &realized
            .getattr("resources_by_logical_key")?
            .call_method0("items")?,
    )? {
        let (key, resources): (Bound<'py, PyAny>, Bound<'py, PyAny>) = entry.extract()?;
        let mut owners: Vec<String> = match names_by_key.get_item(&key)? {
            Some(names) => items(&names)?
                .iter()
                .map(|name| name.extract())
                .collect::<PyResult<_>>()?,
            None => Vec::new(),
        };
        owners.sort();
        logical_resources.push(ManifestLogicalResource {
            logical_type: text(&key.getattr("resource_type")?)?,
            logical_name: key.getattr("name")?.extract()?,
            pipeline_names: owners,
            resources: items(&resources)?
                .iter()
                .map(|resource| {
                    Ok((
                        resource_kind(resource)?,
                        resource.getattr("name")?.extract()?,
                    ))
                })
                .collect::<PyResult<_>>()?,
        });
    }
    let contents = manifest_contents(
        &ManifestTarget {
            project_identity,
            target_name: &target_name,
            target_database,
            is_production,
        },
        pipeline_names,
        &logical_resources,
    );
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    let resource_class: Bound<'py, PyAny> = models.getattr("AdapterManifestResource")?;
    let fields: [&str; 7] = [
        "pipeline_name",
        "logical_type",
        "logical_name",
        "resource_role",
        "resource_database",
        "resource_name",
        "resource_kind",
    ];
    let resources: Vec<Bound<'py, PyAny>> = contents
        .resources
        .into_iter()
        .map(|identity| {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            for (field, value) in fields.iter().zip(identity) {
                keywords.set_item(field, value)?;
            }
            resource_class.call((), Some(&keywords))
        })
        .collect::<PyResult<_>>()?;
    let published_at: Bound<'py, PyAny> = match published_at {
        Some(value) if value.is_truthy()? => value,
        _ => {
            let datetime: Bound<'py, PyModule> = py.import("datetime")?;
            datetime
                .getattr("datetime")?
                .call_method1("now", (datetime.getattr("UTC")?,))?
                .call_method1("strftime", (PUBLISHED_AT_FORMAT,))?
        }
    };
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item(
        "manifest_id",
        py.import("uuid")?.call_method0("uuid4")?.str()?,
    )?;
    keywords.set_item("invocation_id", invocation_id)?;
    keywords.set_item("project_identity", project_identity)?;
    keywords.set_item("target_name", target_name)?;
    keywords.set_item("target_database", target_database)?;
    keywords.set_item("is_production", is_production)?;
    keywords.set_item("project_revision", project_revision)?;
    keywords.set_item("manifest_fingerprint", contents.fingerprint)?;
    keywords.set_item("manifest_version", MANIFEST_VERSION)?;
    keywords.set_item("pipelines", PyTuple::new(py, contents.pipelines)?)?;
    keywords.set_item("resources", PyTuple::new(py, resources)?)?;
    keywords.set_item("tool_version", tool_version)?;
    keywords.set_item("published_at", published_at)?;
    models.getattr("AdapterManifest")?.call((), Some(&keywords))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_dag_json, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_manifest_project_identity, module)?)?;
    module.add_function(wrap_pyfunction!(build_manifest, module)?)?;
    module.add("MANIFEST_VERSION", MANIFEST_VERSION)
}
