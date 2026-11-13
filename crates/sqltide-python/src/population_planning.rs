//! Mode-neutral population planning over the executor's Python shapes: fan-in expansion,
//! physical realization order, replay requests, passive source preparation, in-place row-error
//! policy steps and the direct closure's population plan. The decisions are the native
//! population planner's; this module reads the plans and desired state and builds the results.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use sqltide_compile::relations::RAW_TABLE_NAME_PREFIX;
use sqltide_executor::workflow::step_segment;
use sqltide_sqltext::python_text::{python_isalnum, python_isspace};

use crate::model_sql::chained;
use crate::planner_support::{
    build_adapter_replay_query, build_adapter_resource, build_shadow_adapter_resource,
};
use crate::population::{
    direct_population_scope, plan_population_replays, population_creation_order,
    population_fan_in_roots, population_source_actions,
};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const POPULATION_MODELS: &str = "sqltide.executor.population.models";
const POPULATION_EXCEPTIONS: &str = "sqltide.executor.population.exceptions";
const WORKFLOW_TYPES: &str = "sqltide.executor.workflow.types";
const TEMPLATE_BOUNDARY_TIME: &str = "1970-01-01 00:00:00.000";
const RECOVER_MODEL_VIEW_STEP_PREFIX: &str = "recover_view_";
const RECOVER_DEAD_LETTERS_STEP_PREFIX: &str = "recover_dead_letters_";
const QUARANTINE_LIVE_VIEW_MARKER: &str = "AS __sqt_value_0";

type Value<'py> = Bound<'py, PyAny>;

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

fn is_a(value: &Value<'_>, module: &str, name: &str) -> PyResult<bool> {
    value.is_instance(&class(value.py(), module, name)?)
}

fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    keywords: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in keywords {
        arguments.set_item(keyword, value)?;
    }
    class(py, module, name)?.call((), Some(&arguments))
}

fn replace<'py>(value: &Value<'py>, changes: &[(&str, Value<'py>)]) -> PyResult<Value<'py>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(value.py());
    for (keyword, change) in changes {
        arguments.set_item(keyword, change)?;
    }
    value
        .py()
        .import("dataclasses")?
        .getattr("replace")?
        .call((value,), Some(&arguments))
}

fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn text<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

/// `value or default` for a database name.
fn database_or<'py>(value: Value<'py>, default: &Value<'py>) -> PyResult<Value<'py>> {
    Ok(if value.is_truthy()? {
        value
    } else {
        default.clone()
    })
}

/// Object keys numbered in first-seen order, like `dict.setdefault(key, len(index))`.
struct KeyIndex<'py> {
    index_by_key: Bound<'py, PyDict>,
    keys: Vec<Value<'py>>,
}

impl<'py> KeyIndex<'py> {
    fn new(py: Python<'py>) -> Self {
        Self {
            index_by_key: PyDict::new(py),
            keys: Vec::new(),
        }
    }

    fn add(&mut self, key: Value<'py>) -> PyResult<()> {
        if !self.index_by_key.contains(&key)? {
            self.index_by_key.set_item(&key, self.keys.len())?;
            self.keys.push(key);
        }
        Ok(())
    }

    fn add_all(&mut self, keys: &Value<'py>) -> PyResult<()> {
        for key in items(keys)? {
            self.add(key)?;
        }
        Ok(())
    }

    /// `index_by_key[key]`, raising `KeyError` for an unknown key.
    fn get(&self, key: &Value<'py>) -> PyResult<usize> {
        self.index_by_key.as_any().get_item(key)?.extract()
    }

    fn contains(&self, key: &Value<'py>) -> PyResult<bool> {
        self.index_by_key.contains(key)
    }

    fn indexes(&self, keys: &Value<'py>) -> PyResult<Vec<usize>> {
        items(keys)?.iter().map(|key| self.get(key)).collect()
    }

    fn key(&self, index: usize) -> Value<'py> {
        self.keys[index].clone()
    }

    fn keys_at(&self, py: Python<'py>, indexes: &[usize]) -> PyResult<Value<'py>> {
        tuple(py, indexes.iter().map(|index| self.key(*index)).collect())
    }
}

/// `raise PopulationScopeError(...) from None` for a native scope decision's `ValueError`.
fn scope_error(py: Python<'_>, error: PyErr, keys: &KeyIndex<'_>) -> PyResult<PyErr> {
    if !error.is_instance_of::<PyValueError>(py) {
        return Ok(error);
    }
    let arguments: Value<'_> = error.value(py).getattr("args")?;
    let code: Option<String> = arguments.get_item(0)?.extract().ok();
    let message: String = match code.as_deref() {
        Some("key") => {
            let key: usize = arguments.get_item(1)?.extract()?;
            format!(
                "Population object '{}' has no desired object",
                keys.key(key).getattr("name")?.str()?
            )
        }
        Some("name") => format!(
            "Population relation '{}' has no desired object",
            arguments.get_item(1)?.str()?
        ),
        _ => "Population root has no owning replay root, table, or replay input".to_owned(),
    };
    let translated: Value<'_> =
        class(py, POPULATION_EXCEPTIONS, "PopulationScopeError")?.call1((message,))?;
    chained(&translated, error.value(py), None)?;
    translated.setattr("__suppress_context__", true)?;
    Ok(PyErr::from_value(translated))
}

fn desired_objects<'py>(desired_state: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    items(&desired_state.getattr("objects")?)
}

fn tables<'py>(objects: &[Value<'py>]) -> PyResult<Vec<Value<'py>>> {
    let mut found: Vec<Value<'py>> = Vec::new();
    for object in objects {
        if is_a(object, COMPILE_MODELS, "DesiredTable")? {
            found.push(object.clone());
        }
    }
    Ok(found)
}

/// Materialized views that land or propagate rows, without dead-letter views.
fn replay_views<'py>(objects: &[Value<'py>]) -> PyResult<Vec<Value<'py>>> {
    let mut found: Vec<Value<'py>> = Vec::new();
    for object in objects {
        if is_a(object, COMPILE_MODELS, "DesiredMaterializedView")?
            && !object.getattr("dead_letters")?.is_truthy()?
        {
            found.push(object.clone());
        }
    }
    Ok(found)
}

/// Add direct catch-up roots for in-scope side-reference fan-in targets.
#[pyfunction]
#[pyo3(signature = (*, plan, desired_state))]
pub(crate) fn expand_population_plan<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    desired_state: &Value<'py>,
) -> PyResult<Value<'py>> {
    let objects: Vec<Value<'py>> = desired_objects(desired_state)?;
    let views: Vec<Value<'py>> = replay_views(&objects)?;
    let tables: Vec<Value<'py>> = tables(&objects)?;
    let plan_objects: Vec<Value<'py>> = items(&plan.getattr("objects")?)?;
    let roots: Vec<Value<'py>> = items(&plan.getattr("roots")?)?;
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    for object in &plan_objects {
        index.add(object.getattr("logical_key")?)?;
    }
    for root in &roots {
        index.add(root.getattr("root_key")?)?;
        index.add_all(&root.getattr("affected_keys")?)?;
    }
    for table in &tables {
        index.add(table.getattr("key")?)?;
    }
    for view in &views {
        index.add(view.getattr("key")?)?;
        index.add_all(&view.getattr("deps")?)?;
    }
    let planned: HashSet<usize> = plan_objects
        .iter()
        .map(|object| index.get(&object.getattr("logical_key")?))
        .collect::<PyResult<_>>()?;
    let native_roots: Vec<(usize, Vec<usize>)> = roots
        .iter()
        .map(|root| {
            Ok((
                index.get(&root.getattr("root_key")?)?,
                index.indexes(&root.getattr("affected_keys")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let native_tables: Vec<(String, usize)> = tables
        .iter()
        .map(|table| {
            Ok((
                table.getattr("name")?.extract()?,
                index.get(&table.getattr("key")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let native_views: Vec<(usize, String, String, Vec<usize>)> = views
        .iter()
        .map(|view| {
            Ok((
                index.get(&view.getattr("key")?)?,
                view.getattr("source_table_name")?.extract()?,
                view.getattr("target_table_name")?.extract()?,
                index.indexes(&view.getattr("deps")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let added = match population_fan_in_roots(planned, native_roots, native_tables, native_views) {
        Ok(added) => added,
        Err(error) => return Err(scope_error(py, error, &index)?),
    };
    let mut expanded: Vec<Value<'py>> = roots.clone();
    for (owner, root_key, (target_key, view_key), source_key) in added {
        expanded.push(replace(
            &roots[owner],
            &[
                ("root_key", index.key(root_key)),
                ("affected_keys", index.keys_at(py, &[target_key, view_key])?),
                ("upstream_boundary_key", index.key(source_key)),
                (
                    "persist_watermarks",
                    false.into_pyobject(py)?.to_owned().into_any(),
                ),
            ],
        )?);
    }
    replace(plan, &[("roots", tuple(py, expanded)?)])
}

/// `physical_name_by_key`, plus the active physical table of a materialized view's source table
/// when that table is not staged and the catalog binds its stable name.
fn with_active_source<'py>(
    py: Python<'py>,
    desired: &Value<'py>,
    physical_name_by_key: &Bound<'py, PyDict>,
    target_catalog: Option<&Value<'py>>,
) -> PyResult<Bound<'py, PyDict>> {
    let names: Bound<'py, PyDict> = physical_name_by_key.copy()?;
    let Some(catalog) = target_catalog.filter(|catalog| !catalog.is_none()) else {
        return Ok(names);
    };
    if !is_a(desired, COMPILE_MODELS, "DesiredMaterializedView")? {
        return Ok(names);
    }
    let source: Value<'py> = desired.getattr("source_table_name")?;
    let table_type: Value<'py> = py
        .import("sqltide.compiler.compile.constants")?
        .getattr("DESIRED_OBJECT_TYPE_TABLE")?;
    for key in physical_name_by_key.keys() {
        if key.getattr("object_type")?.eq(&table_type)? && key.getattr("name")?.eq(&source)? {
            return Ok(names);
        }
    }
    let relation: Value<'py> = catalog.call_method1("relation", (&source,))?;
    if relation.is_none() {
        return Ok(names);
    }
    let binding: Value<'py> = relation.getattr("stable_binding_name")?;
    if binding.is_none() {
        return Ok(names);
    }
    let key: Value<'py> = build(
        py,
        COMPILE_MODELS,
        "ObjectKey",
        &[
            ("database", py.None().into_bound(py)),
            ("object_type", table_type),
            ("name", source),
        ],
    )?;
    names.set_item(key, binding)?;
    Ok(names)
}

/// Population resources in the shared realization order, without mutation. With
/// `target_catalog`, a staged materialized view whose source table is not staged reads that
/// table's active physical table: a view bound to a stable name never sees new inserts.
#[pyfunction]
#[pyo3(signature = (*, plan, desired_state, default_database, target_catalog = None))]
pub(crate) fn plan_population_objects<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    desired_state: &Value<'py>,
    default_database: &Value<'py>,
    target_catalog: Option<&Value<'py>>,
) -> PyResult<Value<'py>> {
    let objects: Vec<Value<'py>> = desired_objects(desired_state)?;
    let object_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in &objects {
        object_by_key.set_item(object.getattr("key")?, object)?;
    }
    let physical_name_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for prepared in items(&plan.getattr("objects")?)? {
        physical_name_by_key.set_item(
            prepared.getattr("logical_key")?,
            prepared.getattr("physical_name")?,
        )?;
    }
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    for object in &objects {
        index.add(object.getattr("key")?)?;
        index.add_all(&object.getattr("deps")?)?;
    }
    for key in physical_name_by_key.keys() {
        index.add(key)?;
    }
    let native_objects: Vec<(usize, Vec<usize>)> = objects
        .iter()
        .map(|object| {
            Ok((
                index.get(&object.getattr("key")?)?,
                index.indexes(&object.getattr("deps")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let planned: HashSet<usize> = physical_name_by_key
        .keys()
        .iter()
        .map(|key| index.get(&key))
        .collect::<PyResult<_>>()?;
    let ordered: Vec<usize> = match population_creation_order(native_objects, planned) {
        Ok(ordered) => ordered,
        Err(error) => return Err(scope_error(py, error, &index)?),
    };
    let mut realizations: Vec<Value<'py>> = Vec::new();
    for target in ordered {
        let target_key: Value<'py> = index.key(target);
        let desired: Value<'py> = object_by_key.as_any().get_item(&target_key)?;
        if is_a(&desired, COMPILE_MODELS, "DesiredKafkaTable")? {
            continue;
        }
        let names: Bound<'py, PyDict> =
            with_active_source(py, &desired, &physical_name_by_key, target_catalog)?;
        let resource: Value<'py> = build_shadow_adapter_resource(
            py,
            &desired,
            &physical_name_by_key.as_any().get_item(&target_key)?,
            names.as_any(),
        )?;
        realizations.push(build(
            py,
            POPULATION_MODELS,
            "PopulationRealization",
            &[
                ("resource", resource),
                (
                    "database",
                    database_or(
                        desired.getattr("key")?.getattr("database")?,
                        default_database,
                    )?,
                ),
            ],
        )?);
    }
    tuple(py, realizations)
}

/// `(partition, offset, timestamp, landed at, cursor)` column overrides of one external source.
fn external_columns(config: &Value<'_>) -> PyResult<[Option<String>; 5]> {
    Ok([
        config.getattr("partition_column_name")?.extract()?,
        config.getattr("offset_column_name")?.extract()?,
        config.getattr("timestamp_column_name")?.extract()?,
        config.getattr("landed_at_column_name")?.extract()?,
        config.getattr("cursor_column_name")?.extract()?,
    ])
}

/// One replay request per population root; without `watermarks`, each request is the root's
/// boundary-independent full-replay template.
fn population_replay_requests<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    desired_state: &Value<'py>,
    default_database: &str,
    watermarks: Option<&Value<'py>>,
    boundary_time: &str,
) -> PyResult<Value<'py>> {
    let objects: Vec<Value<'py>> = desired_objects(desired_state)?;
    let plan_objects: Vec<Value<'py>> = items(&plan.getattr("objects")?)?;
    let configs: Vec<Value<'py>> =
        items(&desired_state.getattr("external_source_replay_configs")?)?;
    let roots: Vec<Value<'py>> = items(&plan.getattr("roots")?)?;
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    for object in &objects {
        index.add(object.getattr("key")?)?;
    }
    for prepared in &plan_objects {
        index.add(prepared.getattr("logical_key")?)?;
    }
    for config in &configs {
        index.add(config.getattr("key")?)?;
    }
    for root in &roots {
        index.add(root.getattr("root_key")?)?;
    }
    for root in &roots {
        index.add(root.getattr("upstream_boundary_key")?)?;
    }
    let tables: Vec<Value<'py>> = tables(&objects)?;
    let views: Vec<Value<'py>> = replay_views(&objects)?;
    let native_roots = roots
        .iter()
        .map(|root| {
            Ok((
                index.get(&root.getattr("root_key")?)?,
                index.get(&root.getattr("upstream_boundary_key")?)?,
                root.getattr("replay_lineage_mode")?.str()?.extract()?,
                root.getattr("execution_mode")?.str()?.extract()?,
                root.getattr("forced_start_time")?.extract()?,
                root.getattr("execution_lookback_seconds")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let native_tables = tables
        .iter()
        .map(|table| {
            let key: Value<'py> = table.getattr("key")?;
            let settings: Value<'py> = table.getattr("replay_settings")?;
            let settings: Vec<(String, String)> = if settings.is_truthy()? {
                settings
                    .call_method0("items")?
                    .try_iter()?
                    .map(|item| item?.extract())
                    .collect::<PyResult<_>>()?
            } else {
                Vec::new()
            };
            Ok((
                index.get(&key)?,
                table.getattr("name")?.extract()?,
                key.getattr("database")?.extract()?,
                items(&table.getattr("columns")?)?
                    .iter()
                    .map(|column| column.getattr("name")?.extract())
                    .collect::<PyResult<_>>()?,
                settings,
            ))
        })
        .collect::<PyResult<_>>()?;
    let native_views: Vec<(String, String)> = views
        .iter()
        .map(|view| {
            Ok((
                view.getattr("target_table_name")?.extract()?,
                view.getattr("source_table_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let key_names: Vec<String> = index
        .keys
        .iter()
        .map(|key| key.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    let physical_names: Vec<(usize, String)> = plan_objects
        .iter()
        .map(|prepared| {
            Ok((
                index.get(&prepared.getattr("logical_key")?)?,
                prepared.getattr("physical_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let native_external = configs
        .iter()
        .map(|config| {
            Ok((
                index.get(&config.getattr("key")?)?,
                external_columns(config)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let mut anchors: HashSet<usize> = HashSet::new();
    for key in items(&desired_state.getattr("replay_anchor_keys")?)? {
        if index.contains(&key)? {
            anchors.insert(index.get(&key)?);
        }
    }
    let native_watermarks: Option<Vec<(usize, String, String)>> = match watermarks {
        None => None,
        Some(watermarks) => {
            let mut found: Vec<(usize, String, String)> = Vec::new();
            for watermark in items(watermarks)? {
                let root_key: Value<'py> = watermark.getattr("root_key")?;
                if index.contains(&root_key)? {
                    found.push((
                        index.get(&root_key)?,
                        watermark.getattr("boundary_key")?.extract()?,
                        watermark.getattr("cutoff_value")?.extract()?,
                    ));
                }
            }
            Some(found)
        }
    };
    let (mappings, replay_plans) = plan_population_replays(
        native_roots,
        native_tables,
        native_views,
        key_names,
        physical_names,
        native_external,
        anchors,
        default_database,
        boundary_time,
        native_watermarks,
    )?;
    let physical_mappings: Value<'py> = tuple(
        py,
        mappings
            .into_iter()
            .map(|(logical_name, physical_name)| {
                build(
                    py,
                    ADAPTER_MODELS,
                    "AdapterPhysicalRelationMapping",
                    &[
                        ("logical_name", text(py, &logical_name)),
                        ("physical_name", text(py, &physical_name)),
                    ],
                )
            })
            .collect::<PyResult<_>>()?,
    )?;
    let mut requests: Vec<Value<'py>> = Vec::new();
    for (
        root,
        mode,
        database,
        (root_name, source_name, anchor_name, target_name),
        view_index,
        boundaries,
        [partition, offset, timestamp, landed_at, cursor],
        (lower_bound_mode, window_boundary_time, forced_start_time, lookback_seconds),
        seeds_history,
        target_column_names,
        settings,
        filter_boundaries_at_source,
    ) in replay_plans
    {
        let view: &Value<'py> = &views[view_index];
        let database_value: Value<'py> = text(py, &database);
        let replay_query: Value<'py> = build_adapter_replay_query(
            py,
            &view.getattr("query")?,
            Some(view.getattr("database_template")?),
            &view.getattr("source_table_name")?,
            &database_value,
            &physical_mappings,
        )?;
        let boundaries: Value<'py> = tuple(
            py,
            boundaries
                .into_iter()
                .map(|(boundary_key, cutoff_value, partition_value)| {
                    build(
                        py,
                        ADAPTER_MODELS,
                        "AdapterReplayBoundary",
                        &[
                            ("boundary_key", text(py, &boundary_key)),
                            ("cutoff_value", text(py, &cutoff_value)),
                            (
                                "cutoff_inclusive",
                                true.into_pyobject(py)?.to_owned().into_any(),
                            ),
                            (
                                "partition_value",
                                partition_value.into_pyobject(py)?.into_any(),
                            ),
                        ],
                    )
                })
                .collect::<PyResult<_>>()?,
        )?;
        let seed_mode: &str = if seeds_history {
            "HISTORY_PREFIX"
        } else {
            "NONE"
        };
        let filters: Value<'py> = if filter_boundaries_at_source {
            true.into_pyobject(py)?.to_owned().into_any()
        } else {
            view.getattr("filters_row_errors")?
        };
        let settings: Vec<Value<'py>> = settings
            .into_iter()
            .map(|setting| Ok(setting.into_pyobject(py)?.into_any()))
            .collect::<PyResult<_>>()?;
        let request: Value<'py> = build(
            py,
            ADAPTER_MODELS,
            "AdapterReplayRequest",
            &[
                (
                    "mode",
                    class(py, ADAPTER_TYPES, "AdapterReplayBoundaryMode")?.call1((mode,))?,
                ),
                ("database", database_value),
                (
                    "relations",
                    build(
                        py,
                        ADAPTER_MODELS,
                        "AdapterReplayRelations",
                        &[
                            ("root", text(py, &root_name)),
                            ("source", text(py, &source_name)),
                            ("anchor", text(py, &anchor_name)),
                            ("target", text(py, &target_name)),
                        ],
                    )?,
                ),
                ("replay_query", replay_query),
                ("boundaries", boundaries),
                (
                    "columns",
                    build(
                        py,
                        ADAPTER_MODELS,
                        "AdapterReplayColumns",
                        &[
                            ("partition", text(py, &partition)),
                            ("offset", text(py, &offset)),
                            ("timestamp", text(py, &timestamp)),
                            ("landed_at", text(py, &landed_at)),
                            ("cursor", text(py, &cursor)),
                        ],
                    )?,
                ),
                (
                    "window",
                    build(
                        py,
                        ADAPTER_MODELS,
                        "AdapterReplayWindow",
                        &[
                            (
                                "lower_bound_mode",
                                class(py, ADAPTER_TYPES, "AdapterReplayLowerBoundMode")?
                                    .call1((lower_bound_mode,))?,
                            ),
                            (
                                "lower_bound_inclusive",
                                true.into_pyobject(py)?.to_owned().into_any(),
                            ),
                            ("boundary_time", text(py, &window_boundary_time)),
                            (
                                "forced_start_time",
                                forced_start_time.into_pyobject(py)?.into_any(),
                            ),
                            (
                                "lookback_seconds",
                                lookback_seconds.into_pyobject(py)?.into_any(),
                            ),
                        ],
                    )?,
                ),
                (
                    "seed_mode",
                    class(py, ADAPTER_TYPES, "AdapterReplaySeedMode")?.getattr(seed_mode)?,
                ),
                (
                    "target_column_names",
                    PyTuple::new(py, target_column_names)?.into_any(),
                ),
                ("settings", tuple(py, settings)?),
                ("filter_boundaries_at_source", filters),
            ],
        )?;
        requests.push(tuple(py, vec![index.key(root), request])?);
    }
    tuple(py, requests)
}

/// One boundary-independent full-replay template per population root.
#[pyfunction]
#[pyo3(signature = (*, plan, desired_state, default_database))]
pub(crate) fn build_population_replay_templates<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    desired_state: &Value<'py>,
    default_database: &str,
) -> PyResult<Value<'py>> {
    population_replay_requests(
        py,
        plan,
        desired_state,
        default_database,
        None,
        TEMPLATE_BOUNDARY_TIME,
    )
}

/// One replay request per population root, bounded by the roots' watermarks.
#[pyfunction]
#[pyo3(signature = (*, plan, desired_state, default_database, watermarks, boundary_time))]
fn build_population_replay_requests<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    desired_state: &Value<'py>,
    default_database: &str,
    watermarks: &Value<'py>,
    boundary_time: &str,
) -> PyResult<Value<'py>> {
    population_replay_requests(
        py,
        plan,
        desired_state,
        default_database,
        Some(watermarks),
        boundary_time,
    )
}

/// `(kind, name, landing target)` of one desired object for source planning.
fn source_object(object: &Value<'_>) -> PyResult<(String, String, Option<String>)> {
    let name: String = object.getattr("name")?.extract()?;
    Ok(if is_a(object, COMPILE_MODELS, "DesiredKafkaTable")? {
        ("kafka".to_owned(), name, None)
    } else if is_a(object, COMPILE_MODELS, "DesiredTable")? {
        ("table".to_owned(), name, None)
    } else if is_a(object, COMPILE_MODELS, "DesiredMaterializedView")? {
        (
            "materialized_view".to_owned(),
            name,
            object.getattr("target_table_name")?.extract()?,
        )
    } else {
        ("view".to_owned(), name, None)
    })
}

/// Passive managed sources and deferred landing attachment, without mutation.
#[pyfunction]
#[pyo3(signature = (*, desired_state, default_database, existing_relation_names))]
pub(crate) fn plan_population_sources<'py>(
    py: Python<'py>,
    desired_state: &Value<'py>,
    default_database: &Value<'py>,
    existing_relation_names: HashSet<String>,
) -> PyResult<Value<'py>> {
    let objects: Vec<Value<'py>> = desired_objects(desired_state)?;
    let actions: Vec<&'static str> = population_source_actions(
        objects.iter().map(source_object).collect::<PyResult<_>>()?,
        RAW_TABLE_NAME_PREFIX,
        existing_relation_names,
    )?;
    let mut preserved: Vec<Value<'py>> = Vec::new();
    let mut created: Vec<Value<'py>> = Vec::new();
    let mut landing_views: Vec<Value<'py>> = Vec::new();
    let mut managed_sources: Vec<Value<'py>> = Vec::new();
    let mut realizations: Vec<Value<'py>> = Vec::new();
    for (object, action) in objects.iter().zip(actions) {
        if action == "skip" {
            continue;
        }
        let database = || {
            database_or(
                object.getattr("key")?.getattr("database")?,
                default_database,
            )
        };
        if is_a(object, COMPILE_MODELS, "DesiredKafkaTable")? {
            let resource: Value<'py> = build_adapter_resource(py, object)?;
            if is_a(&resource, ADAPTER_MODELS, "AdapterManagedSource")? {
                managed_sources.push(build(
                    py,
                    POPULATION_MODELS,
                    "PopulationManagedSource",
                    &[("resource", resource), ("database", database()?)],
                )?);
            }
        }
        if action == "preserve" {
            preserved.push(object.getattr("name")?);
            continue;
        }
        created.push(object.getattr("name")?);
        if action == "land" && is_a(object, COMPILE_MODELS, "DesiredMaterializedView")? {
            landing_views.push(object.clone());
            continue;
        }
        realizations.push(build(
            py,
            POPULATION_MODELS,
            "PopulationRealization",
            &[
                ("resource", build_adapter_resource(py, object)?),
                ("database", database()?),
            ],
        )?);
    }
    let preparation: Value<'py> = build(
        py,
        POPULATION_MODELS,
        "PopulationSourcePreparation",
        &[
            ("preserved_relation_names", tuple(py, preserved)?),
            ("created_relation_names", tuple(py, created)?),
            ("landing_views", tuple(py, landing_views)?),
            ("managed_sources", tuple(py, managed_sources)?),
        ],
    )?;
    tuple(py, vec![preparation, tuple(py, realizations)?])
}

fn quarantine<'py>(py: Python<'py>) -> PyResult<Value<'py>> {
    class(py, DISCOVERY_TYPES, "RowErrorPolicy")?.getattr("QUARANTINE")
}

/// Teardown and realization steps of in-place row-error policy changes, never dropping a view
/// that cannot be replaced.
#[pyfunction]
#[pyo3(signature = (*, changes, database, render_view))]
fn row_error_policy_view_steps<'py>(
    py: Python<'py>,
    changes: &Value<'py>,
    database: &str,
    render_view: &Value<'py>,
) -> PyResult<Value<'py>> {
    let quarantine: Value<'py> = quarantine(py)?;
    let teardown: Bound<'py, PyList> = PyList::empty(py);
    let realizations: Bound<'py, PyList> = PyList::empty(py);
    for change in items(changes)? {
        if change.getattr("landing_repair_only")?.is_truthy()? {
            continue;
        }
        let view_sql: Value<'py> = render_view.call1((change.getattr("logical_view_name")?,))?;
        let dead_letter_sql: Value<'py> =
            render_view.call1((change.getattr("logical_dead_letter_view_name")?,))?;
        if view_sql.is_none()
            || (dead_letter_sql.is_none() && change.getattr("desired_policy")?.is(&quarantine))
        {
            continue;
        }
        let view_name: String = change.getattr("view_name")?.str()?.extract()?;
        let dead_letter_view_name: Value<'py> = change.getattr("dead_letter_view_name")?;
        if !dead_letter_sql.is_none() {
            teardown.append((&dead_letter_view_name, false))?;
            realizations.append((&dead_letter_view_name, &dead_letter_sql))?;
        }
        realizations.append((
            format!("replace_{view_name}"),
            format!("DROP TABLE IF EXISTS {database}.{view_name} SYNC"),
        ))?;
        realizations.append((change.getattr("view_name")?, &view_sql))?;
        if dead_letter_sql.is_none() {
            let dead_letter_view_name: String = dead_letter_view_name.str()?.extract()?;
            realizations.append((
                format!("retire_{dead_letter_view_name}"),
                format!("DROP TABLE IF EXISTS {database}.{dead_letter_view_name} SYNC"),
            ))?;
        }
    }
    tuple(py, vec![teardown.into_any(), realizations.into_any()])
}

fn is_word(character: char) -> bool {
    character == '_' || python_isalnum(character)
}

/// Whether a live view definition already projects quarantined rows: Python's
/// `re.search(r"\bAS __sqt_value_0\b", definition)`.
fn projects_quarantine(definition: &str) -> bool {
    definition
        .match_indices(QUARANTINE_LIVE_VIEW_MARKER)
        .any(|(start, found)| {
            let before: Option<char> = definition[..start].chars().next_back();
            let after: Option<char> = definition[start + found.len()..].chars().next();
            !before.is_some_and(is_word) && !after.is_some_and(is_word)
        })
}

/// `text.rstrip().rstrip(";")`.
fn trim_statement_end(text: &str) -> &str {
    text.trim_end_matches(python_isspace).trim_end_matches(';')
}

/// `(step ID, phase, intent, SQL, continue on error)` restoring each previous view and dropping
/// the dead-letter views a policy switch created.
#[pyfunction]
#[pyo3(signature = (*, changes, render_restore, database))]
pub(crate) fn row_error_policy_restore_statements<'py>(
    py: Python<'py>,
    changes: &Value<'py>,
    render_restore: &Value<'py>,
    database: &str,
) -> PyResult<Value<'py>> {
    let quarantine: Value<'py> = quarantine(py)?;
    let finalization: String = class(py, WORKFLOW_TYPES, "WorkflowPhase")?
        .getattr("FINALIZATION")?
        .str()?
        .extract()?;
    let mutation: String = class(py, WORKFLOW_TYPES, "StatementIntent")?
        .getattr("MUTATION")?
        .str()?
        .extract()?;
    let mut switched: Vec<Value<'py>> = Vec::new();
    for change in items(changes)? {
        if !change.getattr("previous_view_definition")?.is_none()
            && !change.getattr("landing_repair_only")?.is_truthy()?
        {
            switched.push(change);
        }
    }
    let previous = |change: &Value<'py>| -> PyResult<String> {
        let definition: Value<'py> = change.getattr("previous_view_definition")?;
        Ok(if definition.is_truthy()? {
            definition.extract()?
        } else {
            String::new()
        })
    };
    let statements: Bound<'py, PyList> = PyList::empty(py);
    for change in &switched {
        let view_name: String = change.getattr("view_name")?.extract()?;
        let restored: String = render_restore.call1((previous(change)?,))?.extract()?;
        statements.append((
            format!(
                "{RECOVER_MODEL_VIEW_STEP_PREFIX}{}",
                step_segment(&view_name)
            ),
            &finalization,
            &mutation,
            format!("{};", trim_statement_end(&restored)),
            false,
        ))?;
    }
    for change in &switched {
        if !change.getattr("desired_policy")?.is(&quarantine)
            || projects_quarantine(&previous(change)?)
        {
            continue;
        }
        let view_name: String = change.getattr("view_name")?.extract()?;
        let dead_letter_view_name: String = change.getattr("dead_letter_view_name")?.extract()?;
        statements.append((
            format!(
                "{RECOVER_DEAD_LETTERS_STEP_PREFIX}{}",
                step_segment(&view_name)
            ),
            &finalization,
            &mutation,
            format!("DROP TABLE IF EXISTS {database}.{dead_letter_view_name} SYNC;"),
            false,
        ))?;
    }
    Ok(statements.into_any())
}

/// Every workflow step an in-place row-error policy change runs.
fn change_step_ids(change: &Value<'_>) -> PyResult<HashSet<String>> {
    let view_name: String = change.getattr("view_name")?.extract()?;
    let dead_letter_view_name: String = change.getattr("dead_letter_view_name")?.extract()?;
    let dead_letters: String = step_segment(&dead_letter_view_name);
    let mut steps: HashSet<String> = HashSet::from([
        "wait_for_live_stabilization".to_owned(),
        format!("drop_{dead_letters}"),
        format!("realize_{dead_letters}"),
        format!("realize_{}", step_segment(&format!("replace_{view_name}"))),
        format!("realize_{}", step_segment(&view_name)),
        format!(
            "realize_{}",
            step_segment(&format!("retire_{dead_letter_view_name}"))
        ),
    ]);
    for landing in items(&change.getattr("landing_view_names")?)? {
        let segment: String = step_segment(&landing.extract::<String>()?);
        steps.insert(format!("pause_source_{segment}"));
        steps.insert(format!("activate_source_{segment}"));
    }
    Ok(steps)
}

/// The failure text naming the models a failed policy switch affects, or `None` outside one.
#[pyfunction]
#[pyo3(signature = (*, changes, failed_step_id, cause))]
fn row_error_policy_failure_message<'py>(
    py: Python<'py>,
    changes: &Value<'py>,
    failed_step_id: &str,
    cause: &Value<'py>,
) -> PyResult<Option<String>> {
    let mut models: Vec<String> = Vec::new();
    for change in items(changes)? {
        if change_step_ids(&change)?.contains(failed_step_id) {
            models.push(change.getattr("model_key")?.getattr("name")?.extract()?);
        }
    }
    if models.is_empty() {
        return Ok(None);
    }
    models.sort();
    let named: String = models
        .iter()
        .map(|model| format!("'{model}'"))
        .collect::<Vec<_>>()
        .join(", ");
    let details: Bound<'py, PyList> = PyList::new(py, [cause.str()?])?;
    if let Ok(notes) = cause.getattr("__notes__") {
        for note in items(&notes)? {
            details.append(note)?;
        }
    }
    let joined: String = PyString::new(py, "; ")
        .call_method1("join", (details,))?
        .extract()?;
    Ok(Some(format!(
        "Row-error policy switch failed for model {named}: {joined}"
    )))
}

/// A dead-letter view targeting the metadata database when it differs from the run database.
#[pyfunction]
#[pyo3(signature = (*, resource, database, metadata_database))]
pub(crate) fn with_metadata_dead_letter_target<'py>(
    resource: &Value<'py>,
    database: &Value<'py>,
    metadata_database: &Value<'py>,
) -> PyResult<Value<'py>> {
    if !is_a(resource, ADAPTER_MODELS, "AdapterMaterializedView")?
        || !resource.getattr("dead_letters")?.is_truthy()?
        || metadata_database.eq(database)?
    {
        return Ok(resource.clone());
    }
    let target: String = format!(
        "{}.{}",
        metadata_database.str()?,
        resource.getattr("target_relation_name")?.str()?
    );
    replace(
        resource,
        &[("target_relation_name", text(resource.py(), &target))],
    )
}

/// `(kind, name)` of one realized model resource for direct population scope.
fn direct_resource(resource: &Value<'_>) -> PyResult<(String, String)> {
    let kind: &str = if is_a(resource, ADAPTER_MODELS, "AdapterTable")? {
        "table"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterMaterializedView")? {
        "materialized_view"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterView")? {
        "view"
    } else {
        return Ok(("other".to_owned(), String::new()));
    };
    Ok((kind.to_owned(), resource.getattr("name")?.extract()?))
}

/// The direct closure's logical scope mapped to identity-named physical population.
#[pyfunction]
#[pyo3(signature = (*, plan, realized_project))]
pub(crate) fn build_direct_population_plan<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    realized_project: &Value<'py>,
) -> PyResult<Value<'py>> {
    let desired_state: Value<'py> = realized_project.getattr("desired_state")?;
    let mut desired: Vec<Value<'py>> = Vec::new();
    for object in desired_objects(&desired_state)? {
        if is_a(&object, COMPILE_MODELS, "DesiredTable")?
            || is_a(&object, COMPILE_MODELS, "DesiredMaterializedView")?
            || is_a(&object, COMPILE_MODELS, "DesiredView")?
        {
            desired.push(object);
        }
    }
    let configs: Vec<Value<'py>> =
        items(&desired_state.getattr("external_source_replay_configs")?)?;
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    for object in &desired {
        index.add(object.getattr("key")?)?;
    }
    for config in &configs {
        index.add(config.getattr("key")?)?;
    }
    let replay_roots: Vec<Value<'py>> = items(&plan.getattr("replay_roots")?)?;
    let mut models: KeyIndex<'py> = KeyIndex::new(py);
    models.add_all(&plan.getattr("execution_scope")?)?;
    for root in &replay_roots {
        models.add(root.getattr("model_key")?)?;
        models.add_all(&root.getattr("propagated_model_keys")?)?;
    }
    let native_desired: Vec<(String, usize)> = desired
        .iter()
        .map(|object| {
            Ok((
                object.getattr("name")?.extract()?,
                index.get(&object.getattr("key")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let resources_by_key: Value<'py> = realized_project.getattr("resources_by_logical_key")?;
    let native_resources: Vec<Vec<(String, String)>> = models
        .keys
        .iter()
        .map(|key| {
            items(&resources_by_key.get_item(key)?)?
                .iter()
                .map(direct_resource)
                .collect()
        })
        .collect::<PyResult<_>>()?;
    let execution_scope: Vec<usize> = models.indexes(&plan.getattr("execution_scope")?)?;
    let native_roots: Vec<(usize, Vec<usize>, String)> = replay_roots
        .iter()
        .map(|root| {
            Ok((
                models.get(&root.getattr("model_key")?)?,
                models.indexes(&root.getattr("propagated_model_keys")?)?,
                root.getattr("driving_input_relation_name")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let external_tables: Vec<(String, usize)> = configs
        .iter()
        .map(|config| {
            Ok((
                config.getattr("table_name")?.extract()?,
                index.get(&config.getattr("key")?)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let (planned, scoped_roots) = match direct_population_scope(
        native_desired,
        native_resources,
        execution_scope,
        native_roots,
        external_tables,
    ) {
        Ok(scope) => scope,
        Err(error) => return Err(scope_error(py, error, &index)?),
    };
    let forced_start_time: Value<'py> = plan.getattr("effective_start_time")?;
    let roots: Vec<Value<'py>> = replay_roots
        .iter()
        .zip(scoped_roots)
        .map(|(root, (root_key, affected, anchor))| {
            build(
                py,
                POPULATION_MODELS,
                "PopulationRoot",
                &[
                    ("root_key", index.key(root_key)),
                    ("affected_keys", index.keys_at(py, &affected)?),
                    ("upstream_boundary_key", index.key(anchor)),
                    ("replay_lineage_mode", root.getattr("replay_boundary_mode")?),
                    ("forced_start_time", forced_start_time.clone()),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let objects: Vec<Value<'py>> = planned
        .into_iter()
        .map(|planned| {
            let key: Value<'py> = index.key(planned);
            build(
                py,
                POPULATION_MODELS,
                "PopulationObject",
                &[
                    ("physical_name", key.getattr("name")?),
                    ("logical_key", key),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        POPULATION_MODELS,
        "PopulationPlan",
        &[
            ("execution_id", text(py, "direct")),
            ("roots", tuple(py, roots)?),
            ("objects", tuple(py, objects)?),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(expand_population_plan, module)?)?;
    module.add_function(wrap_pyfunction!(plan_population_objects, module)?)?;
    module.add_function(wrap_pyfunction!(build_population_replay_templates, module)?)?;
    module.add_function(wrap_pyfunction!(build_population_replay_requests, module)?)?;
    module.add_function(wrap_pyfunction!(plan_population_sources, module)?)?;
    module.add_function(wrap_pyfunction!(row_error_policy_view_steps, module)?)?;
    module.add_function(wrap_pyfunction!(
        row_error_policy_restore_statements,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(row_error_policy_failure_message, module)?)?;
    module.add_function(wrap_pyfunction!(with_metadata_dead_letter_target, module)?)?;
    module.add_function(wrap_pyfunction!(build_direct_population_plan, module)?)
}
