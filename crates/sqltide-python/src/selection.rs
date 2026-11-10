//! Command-line model selection: named models and pipelines, their downstream execution closure,
//! the pipeline sources and dependencies they need, and the replay lineage mode the selected
//! pipelines agree on. Keys stay the project's Python `ObjectKey` and `LogicalResourceKey`
//! values, iterated in the same set order the selection always used.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PySet, PyTuple};

use crate::direct_build::{Value, build, class, is_a, items, text, tuple};
use crate::graph::resolve_named_model_keys;

const NATIVE: &str = "sqltide._native";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_TYPES: &str = "sqltide.compiler.compile.types";
const GRAPH_TYPES: &str = "sqltide.compiler.graph.types";
const GRAPH_CONSTANTS: &str = "sqltide.compiler.graph.constants";
const GRAPH_EXCEPTIONS: &str = "sqltide.compiler.graph.exceptions";
const SELECTION_MODELS: &str = "sqltide.cli.selection.models";
const CLI_EXCEPTIONS: &str = "sqltide.cli.entry.exceptions";

fn cli_user_error(py: Python<'_>, message: String) -> PyErr {
    match class(py, CLI_EXCEPTIONS, "CliUserError").and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn frozenset<'py>(py: Python<'py>, values: &[Value<'py>]) -> PyResult<Bound<'py, PyFrozenSet>> {
    PyFrozenSet::new(py, values)
}

fn model_keys<'py>(container: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    items(&container.getattr("models")?)?
        .iter()
        .map(|model| model.getattr("key"))
        .collect()
}

fn object_key<'py>(py: Python<'py>, object_type: &str, name: Value<'py>) -> PyResult<Value<'py>> {
    build(
        py,
        COMPILE_MODELS,
        "ObjectKey",
        &[
            ("database", py.None().into_bound(py)),
            ("object_type", text(py, object_type)),
            ("name", name),
        ],
    )
}

fn resource_key<'py>(py: Python<'py>, resource: &Value<'py>) -> PyResult<Value<'py>> {
    let object_type: &str = if is_a(resource, ADAPTER_MODELS, "AdapterManagedSource")? {
        "kafka_table"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterMaterializedView")? {
        "materialized_view"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterView")? {
        "view"
    } else {
        "table"
    };
    object_key(py, object_type, resource.getattr("name")?)
}

/// The physical table or view key of each logical model, refusing other resources.
fn physical_model_keys<'py>(
    py: Python<'py>,
    realized_project: &Value<'py>,
    logical_model_keys: &Bound<'py, PyFrozenSet>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    let model_type: Value<'py> =
        class(py, COMPILE_TYPES, "LogicalResourceType")?.getattr("MODEL")?;
    let resources_by_key: Value<'py> = realized_project.getattr("resources_by_logical_key")?;
    let table: Value<'py> = class(py, ADAPTER_MODELS, "AdapterTable")?;
    let view: Value<'py> = class(py, ADAPTER_MODELS, "AdapterView")?;
    let keys: Bound<'py, PySet> = PySet::empty(py)?;
    for logical_key in logical_model_keys.iter() {
        if logical_key.getattr("resource_type")?.ne(&model_type)? {
            return Err(cli_user_error(
                py,
                format!(
                    "Cannot select non-model logical resource '{}'",
                    logical_key.getattr("name")?.str()?
                ),
            ));
        }
        let mut found: Option<Value<'py>> = None;
        for resource in items(&resources_by_key.get_item(&logical_key)?)? {
            if resource.is_instance(&table)? || resource.is_instance(&view)? {
                found = Some(resource);
                break;
            }
        }
        let Some(resource) = found else {
            return Err(pyo3::exceptions::PyStopIteration::new_err(()));
        };
        keys.add(resource_key(py, &resource)?)?;
    }
    PyFrozenSet::new(py, keys.iter())
}

fn pipeline_source_keys<'py>(
    py: Python<'py>,
    compiled_pipeline: &Value<'py>,
    realized_project: &Value<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let source: Value<'py> = compiled_pipeline.getattr("source")?;
    if source.is_none() {
        return Ok(Vec::new());
    }
    let source_key: Value<'py> = source.getattr("key")?;
    let mut keys: Vec<Value<'py>> = Vec::new();
    for resource in items(
        &realized_project
            .getattr("resources_by_logical_key")?
            .get_item(&source_key)?,
    )? {
        keys.push(resource_key(py, &resource)?);
    }
    keys.push(object_key(
        py,
        "table",
        realized_project
            .getattr("relation_name_by_logical_key")?
            .get_item(&source_key)?,
    )?);
    Ok(keys)
}

/// Selected models, their pipelines' sources, everything downstream of them and every
/// dependency of what is included.
fn expand_included_keys<'py>(
    py: Python<'py>,
    compiled_pipelines: &[Value<'py>],
    realized_project: &Value<'py>,
    desired_state: &Value<'py>,
    selected_model_keys: &Bound<'py, PyFrozenSet>,
) -> PyResult<Bound<'py, PySet>> {
    let reverse_deps: Value<'py> = py
        .import(NATIVE)?
        .getattr("DesiredGraph")?
        .call1((desired_state,))?
        .call_method0("reverse_deps")?;
    let object_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in items(&desired_state.getattr("objects")?)? {
        if is_a(&object, COMPILE_MODELS, "DesiredTable")?
            || is_a(&object, COMPILE_MODELS, "DesiredMaterializedView")?
            || is_a(&object, COMPILE_MODELS, "DesiredView")?
        {
            object_by_key.set_item(object.getattr("key")?, object)?;
        }
    }
    let included: Bound<'py, PySet> = PySet::new(py, selected_model_keys.iter())?;
    for compiled_pipeline in compiled_pipelines {
        let pipeline_model_keys: Bound<'py, PyFrozenSet> = physical_model_keys(
            py,
            realized_project,
            &frozenset(py, &model_keys(compiled_pipeline)?)?,
        )?;
        if !pipeline_model_keys
            .call_method1("__and__", (selected_model_keys,))?
            .is_truthy()?
        {
            continue;
        }
        for key in pipeline_source_keys(py, compiled_pipeline, realized_project)? {
            included.add(key)?;
        }
    }
    let mut stack: Vec<Value<'py>> = selected_model_keys.iter().collect();
    let empty: Value<'py> = PyTuple::empty(py).into_any();
    while let Some(current) = stack.pop() {
        for downstream in items(&reverse_deps.call_method1("get", (current, &empty))?)? {
            if included.contains(&downstream)? {
                continue;
            }
            included.add(&downstream)?;
            stack.push(downstream);
        }
    }
    let mut changed: bool = true;
    while changed {
        changed = false;
        for key in items(&PyTuple::new(py, included.iter())?.into_any())? {
            let Some(object) = object_by_key.get_item(&key)? else {
                continue;
            };
            for dependency in items(&object.getattr("deps")?)? {
                if included.contains(&dependency)? {
                    continue;
                }
                included.add(dependency)?;
                changed = true;
            }
        }
    }
    Ok(included)
}

fn filter_desired_state<'py>(
    py: Python<'py>,
    desired_state: &Value<'py>,
    included: &Bound<'py, PyFrozenSet>,
) -> PyResult<Value<'py>> {
    let ordered: Value<'py> = py
        .import(NATIVE)?
        .getattr("DesiredGraph")?
        .call1((desired_state,))?
        .call_method1("order", (included,))?;
    let object_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in items(&desired_state.getattr("objects")?)? {
        let key: Value<'py> = object.getattr("key")?;
        if included.contains(&key)? {
            object_by_key.set_item(key, object)?;
        }
    }
    let mut objects: Vec<Value<'py>> = Vec::new();
    for key in items(&ordered)? {
        objects.push(object_by_key.as_any().get_item(key)?);
    }
    let kept = |name: &str| -> PyResult<Vec<Value<'py>>> {
        let mut kept: Vec<Value<'py>> = Vec::new();
        for key in items(&desired_state.getattr(name)?)? {
            if included.contains(&key)? {
                kept.push(key);
            }
        }
        Ok(kept)
    };
    let mut configs: Vec<Value<'py>> = Vec::new();
    for config in items(&desired_state.getattr("external_source_replay_configs")?)? {
        if included.contains(config.getattr("key")?)? {
            configs.push(config);
        }
    }
    build(
        py,
        COMPILE_MODELS,
        "DesiredState",
        &[
            ("objects", tuple(py, objects)?),
            (
                "replay_anchor_keys",
                frozenset(py, &kept("replay_anchor_keys")?)?.into_any(),
            ),
            (
                "mutable_ref_warning_keys",
                frozenset(py, &kept("mutable_ref_warning_keys")?)?.into_any(),
            ),
            ("external_source_replay_configs", tuple(py, configs)?),
        ],
    )
}

/// The one replay lineage mode the selected pipelines declare, or `None` without one.
fn resolve_replay_lineage_mode<'py>(
    py: Python<'py>,
    compiled_pipelines: &[Value<'py>],
    selected_model_keys: &Bound<'py, PyFrozenSet>,
) -> PyResult<Value<'py>> {
    let modes: Bound<'py, PySet> = PySet::empty(py)?;
    let mut selected: Vec<(String, Value<'py>)> = Vec::new();
    for compiled_pipeline in compiled_pipelines {
        let pipeline_model_keys: Bound<'py, PyFrozenSet> =
            frozenset(py, &model_keys(compiled_pipeline)?)?;
        if !selected_model_keys.is_empty()
            && !pipeline_model_keys
                .call_method1("__and__", (selected_model_keys,))?
                .is_truthy()?
        {
            continue;
        }
        let mode: Value<'py> = compiled_pipeline.getattr("effective_replay_lineage_mode")?;
        if mode.is_none() {
            continue;
        }
        modes.add(&mode)?;
        selected.push((
            compiled_pipeline
                .getattr("pipeline")?
                .getattr("name")?
                .extract()?,
            mode,
        ));
    }
    if modes.len() > 1 {
        let mut details: Vec<(String, String)> = selected
            .iter()
            .map(|(name, mode)| Ok((name.clone(), mode.str()?.extract()?)))
            .collect::<PyResult<_>>()?;
        details.sort();
        let details: Vec<String> = details
            .into_iter()
            .map(|(name, mode)| format!("{name}={mode}"))
            .collect();
        return Err(cli_user_error(
            py,
            format!(
                "Selected pipelines disagree on replay_lineage_mode: {}",
                details.join(", ")
            ),
        ));
    }
    Ok(modes
        .iter()
        .next()
        .unwrap_or_else(|| py.None().into_bound(py)))
}

/// The logical model keys named by `selectors`, refusing unknown names as a user error.
#[pyfunction]
#[pyo3(signature = (*, compiled_pipelines, selectors))]
fn resolve_selected_logical_model_keys<'py>(
    py: Python<'py>,
    compiled_pipelines: &Value<'py>,
    selectors: &Value<'py>,
) -> PyResult<Bound<'py, PyFrozenSet>> {
    match resolve_named_model_keys(py, compiled_pipelines, selectors) {
        Err(error) if error.is_instance(py, &class(py, GRAPH_EXCEPTIONS, "GraphInputError")?) => {
            // `raise CliUserError(str(error)) from error`.
            let translated: PyErr = cli_user_error(py, error.value(py).str()?.extract()?);
            let cause: Value<'py> = error.into_value(py).into_bound(py).into_any();
            let value: Value<'py> = translated.value(py).clone().into_any();
            value.setattr("__context__", &cause)?;
            value.setattr("__cause__", &cause)?;
            Err(translated)
        }
        resolved => resolved,
    }
}

/// The desired state, selected keys and replay lineage mode of a command's model selectors.
#[pyfunction]
#[pyo3(signature = (*, realized_project, graph, selectors))]
fn resolve_selection<'py>(
    py: Python<'py>,
    realized_project: &Value<'py>,
    graph: &Value<'py>,
    selectors: &Value<'py>,
) -> PyResult<Value<'py>> {
    let project: Value<'py> = realized_project.getattr("project")?;
    let compiled_pipelines: Vec<Value<'py>> = items(&project.getattr("pipelines")?)?;
    let full_desired_state: Value<'py> = realized_project.getattr("desired_state")?;
    let empty: Bound<'py, PyFrozenSet> = PyFrozenSet::empty(py)?;
    if !selectors.is_truthy()? {
        let execution: Bound<'py, PyFrozenSet> = frozenset(py, &model_keys(&project)?)?;
        return build(
            py,
            SELECTION_MODELS,
            "SelectionResolution",
            &[
                ("desired_state", full_desired_state),
                ("selected_logical_model_keys", empty.clone().into_any()),
                ("selected_model_keys", empty.clone().into_any()),
                (
                    "replay_lineage_mode",
                    resolve_replay_lineage_mode(py, &compiled_pipelines, &empty)?,
                ),
                ("execution_logical_model_keys", execution.into_any()),
            ],
        );
    }
    let selected_logical: Bound<'py, PyFrozenSet> =
        resolve_selected_logical_model_keys(py, &project.getattr("pipelines")?, selectors)?;
    let selected_model_keys: Bound<'py, PyFrozenSet> =
        physical_model_keys(py, realized_project, &selected_logical)?;
    let model_type: Value<'py> =
        class(py, COMPILE_TYPES, "LogicalResourceType")?.getattr("MODEL")?;
    let reachable: Value<'py> = py.import(NATIVE)?.getattr("collect_reachable_keys")?.call(
        (),
        Some(&{
            let arguments: Bound<'py, PyDict> = PyDict::new(py);
            arguments.set_item("graph", graph)?;
            arguments.set_item("root_keys", &selected_logical)?;
            arguments.set_item(
                "direction",
                class(py, GRAPH_TYPES, "GraphTraversalDirection")?.getattr("DOWNSTREAM")?,
            )?;
            arguments.set_item(
                "edge_types",
                class(py, GRAPH_CONSTANTS, "ALL_DEPENDENCY_EDGE_TYPES")?,
            )?;
            arguments
        }),
    )?;
    let mut execution: Vec<Value<'py>> = Vec::new();
    for key in items(&reachable)? {
        if key.getattr("resource_type")?.eq(&model_type)? {
            execution.push(key);
        }
    }
    let execution_logical: Bound<'py, PyFrozenSet> = frozenset(py, &execution)?;
    let execution_model_keys: Bound<'py, PyFrozenSet> =
        physical_model_keys(py, realized_project, &execution_logical)?;
    let included: Bound<'py, PySet> = expand_included_keys(
        py,
        &compiled_pipelines,
        realized_project,
        &full_desired_state,
        &execution_model_keys,
    )?;
    let included: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, included.iter())?;
    build(
        py,
        SELECTION_MODELS,
        "SelectionResolution",
        &[
            (
                "desired_state",
                filter_desired_state(py, &full_desired_state, &included)?,
            ),
            ("selected_logical_model_keys", selected_logical.into_any()),
            ("selected_model_keys", selected_model_keys.into_any()),
            (
                "replay_lineage_mode",
                resolve_replay_lineage_mode(py, &compiled_pipelines, &execution_logical)?,
            ),
            ("execution_logical_model_keys", execution_logical.into_any()),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        resolve_selected_logical_model_keys,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(resolve_selection, module)?)
}
