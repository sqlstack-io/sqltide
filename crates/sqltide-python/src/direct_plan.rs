//! Python bindings for direct-mode planning over a realized project and warehouse snapshot.

use std::collections::HashSet;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyIndexError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use sqltide_planner::deployment::{MUTABLE_REF_WARNING_CODE, MUTABLE_REF_WARNING_MESSAGE};

use crate::planner::{
    CatalogArgument, DirectPlanResult, ExternalArgument, FingerprintArgument, ModelArgument,
    NodeArgument, run_direct_plan,
};

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const CATALOG_MATERIALIZED_VIEW_ENGINE: &str = "MaterializedView";

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(value.str()?.to_string())
}

fn keyword_call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(callable.py());
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

/// `value or None` for an optional text attribute.
fn truthy_text(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_truthy()? {
        value.extract().map(Some)
    } else {
        Ok(None)
    }
}

fn index_of(index: &Bound<'_, PyDict>, key: &Bound<'_, PyAny>) -> PyResult<usize> {
    index
        .get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))?
        .extract()
}

/// What the direct planner reads about one project, keyed like the Python models.
struct Project<'py> {
    keys: Vec<Bound<'py, PyAny>>,
    index: Bound<'py, PyDict>,
    model_by_key: Bound<'py, PyDict>,
    lineage_by_key: Bound<'py, PyDict>,
    pipeline_by_model_key: Bound<'py, PyDict>,
    postgres_source_names: HashSet<String>,
}

impl<'py> Project<'py> {
    fn new(
        py: Python<'py>,
        graph: &Bound<'py, PyAny>,
        realized_project: &Bound<'py, PyAny>,
    ) -> PyResult<Self> {
        let keys: Vec<Bound<'py, PyAny>> = graph
            .getattr("ordered_keys")?
            .try_iter()?
            .collect::<PyResult<_>>()?;
        let index: Bound<'py, PyDict> = PyDict::new(py);
        for (position, key) in keys.iter().enumerate() {
            index.set_item(key, position)?;
        }
        let project: Bound<'py, PyAny> = realized_project.getattr("project")?;
        let model_by_key: Bound<'py, PyDict> = PyDict::new(py);
        for model in project.getattr("models")?.try_iter()? {
            let model: Bound<'py, PyAny> = model?;
            model_by_key.set_item(model.getattr("key")?, model)?;
        }
        let postgres_class = py
            .import(DISCOVERY_MODELS)?
            .getattr("PostgresRefreshSourceStep")?;
        let mut postgres_source_names: HashSet<String> = HashSet::new();
        for source in project.getattr("sources")?.try_iter()? {
            let source: Bound<'py, PyAny> = source?;
            if source.getattr("source")?.is_instance(&postgres_class)? {
                postgres_source_names.insert(source.getattr("key")?.getattr("name")?.extract()?);
            }
        }
        let lineage_by_key: Bound<'py, PyDict> = PyDict::new(py);
        let pipeline_by_model_key: Bound<'py, PyDict> = PyDict::new(py);
        for pipeline in project.getattr("pipelines")?.try_iter()? {
            let pipeline: Bound<'py, PyAny> = pipeline?;
            let models: Vec<Bound<'py, PyAny>> = pipeline
                .getattr("models")?
                .try_iter()?
                .collect::<PyResult<_>>()?;
            let name: Bound<'py, PyAny> = pipeline.getattr("pipeline")?.getattr("name")?;
            for model in &models {
                let key: Bound<'py, PyAny> = model.getattr("key")?;
                if !pipeline_by_model_key.contains(&key)? {
                    pipeline_by_model_key.set_item(key, &name)?;
                }
            }
            let source: Bound<'py, PyAny> = pipeline.getattr("source")?;
            let lineage: Bound<'py, PyAny> = pipeline.getattr("effective_replay_lineage_mode")?;
            if source.is_none() || lineage.is_none() {
                continue;
            }
            lineage_by_key.set_item(source.getattr("key")?, &lineage)?;
            for model in &models {
                lineage_by_key.set_item(model.getattr("key")?, &lineage)?;
            }
        }
        Ok(Self {
            keys,
            index,
            model_by_key,
            lineage_by_key,
            pipeline_by_model_key,
            postgres_source_names,
        })
    }

    fn indexes(&self, keys: &Bound<'py, PyAny>) -> PyResult<HashSet<usize>> {
        let mut found: HashSet<usize> = HashSet::new();
        for key in keys.try_iter()? {
            if let Some(position) = self.index.get_item(key?)? {
                found.insert(position.extract()?);
            }
        }
        Ok(found)
    }

    fn node(
        &self,
        py: Python<'py>,
        graph: &Bound<'py, PyAny>,
        realized_project: &Bound<'py, PyAny>,
        key: &Bound<'py, PyAny>,
    ) -> PyResult<NodeArgument> {
        let adapter_models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
        let table_model = py.import(COMPILE_MODELS)?.getattr("CompiledTableModel")?;
        let model: Option<ModelArgument> = match self.model_by_key.get_item(key)? {
            None => None,
            Some(model) => {
                let is_table: bool = model.is_instance(&table_model)?;
                Some((
                    is_table,
                    is_table && model.getattr("has_aggregate_semantics")?.is_truthy()?,
                    model.getattr("query")?.extract()?,
                    if is_table {
                        let replay: Bound<'py, PyAny> = model
                            .getattr("transform")?
                            .getattr("execution_settings")?
                            .getattr("replay")?;
                        if replay.is_truthy()? {
                            replay
                                .call_method0("items")?
                                .try_iter()?
                                .map(|item| item?.extract())
                                .collect::<PyResult<_>>()?
                        } else {
                            Vec::new()
                        }
                    } else {
                        Vec::new()
                    },
                ))
            }
        };
        let resources: Vec<Bound<'py, PyAny>> = realized_project
            .getattr("resources_by_logical_key")?
            .call_method1("get", (key, PyTuple::empty(py)))?
            .try_iter()?
            .collect::<PyResult<_>>()?;
        let table_class = adapter_models.getattr("AdapterTable")?;
        let materialized_view_class = adapter_models.getattr("AdapterMaterializedView")?;
        let view_class = adapter_models.getattr("AdapterView")?;
        let resource_entries: Vec<(String, Option<String>)> = resources
            .iter()
            .map(|resource| {
                let kind: Option<&str> = if resource.is_instance(&table_class)? {
                    Some("table")
                } else if resource.is_instance(&materialized_view_class)? {
                    Some(if resource.getattr("dead_letters")?.is_truthy()? {
                        "dead_letter_view"
                    } else {
                        "materialized_view"
                    })
                } else if resource.is_instance(&view_class)? {
                    Some("view")
                } else {
                    None
                };
                Ok((
                    resource.getattr("name")?.extract()?,
                    kind.map(str::to_owned),
                ))
            })
            .collect::<PyResult<_>>()?;
        let relation_name: String = realized_project
            .getattr("relation_name_by_logical_key")?
            .get_item(key)?
            .extract()?;
        let name: String = key.getattr("name")?.extract()?;
        let edges = |attribute: &str| -> PyResult<Vec<Bound<'py, PyAny>>> {
            graph
                .getattr(attribute)?
                .call_method1("get", (key, PyTuple::empty(py)))?
                .try_iter()?
                .collect()
        };
        let upstream: Vec<(usize, bool)> = edges("upstream_edges_by_key")?
            .iter()
            .map(|edge| {
                Ok((
                    index_of(&self.index, &edge.getattr("upstream_key")?)?,
                    text(&edge.getattr("edge_type")?)? == "driving_input",
                ))
            })
            .collect::<PyResult<_>>()?;
        let downstream: Vec<usize> = edges("downstream_edges_by_key")?
            .iter()
            .map(|edge| index_of(&self.index, &edge.getattr("downstream_key")?))
            .collect::<PyResult<_>>()?;
        Ok((
            name.clone(),
            model,
            !resources.is_empty(),
            relation_name,
            resource_entries,
            self.lineage_by_key
                .get_item(key)?
                .map(|lineage| text(&lineage))
                .transpose()?,
            self.postgres_source_names.contains(&name),
            self.pipeline_by_model_key
                .get_item(key)?
                .map(|pipeline| pipeline.extract())
                .transpose()?,
            upstream,
            downstream,
        ))
    }
}

/// Plan the complete selected downstream closure of `realized_project` against `snapshot`
/// without change pruning, as a `DirectPlan`. A violated planning rule raises
/// `DirectPlanError`.
#[pyfunction]
#[pyo3(signature = (
    *,
    graph,
    realized_project,
    snapshot,
    database,
    selected_model_keys,
    selection_mode,
    include_missing_upstream,
    effective_start_time,
    source_drift_selections,
))]
#[allow(clippy::too_many_arguments)]
fn plan_direct_build<'py>(
    py: Python<'py>,
    graph: &Bound<'py, PyAny>,
    realized_project: &Bound<'py, PyAny>,
    snapshot: &Bound<'py, PyAny>,
    database: &str,
    selected_model_keys: &Bound<'py, PyAny>,
    selection_mode: &Bound<'py, PyAny>,
    include_missing_upstream: bool,
    effective_start_time: Option<String>,
    source_drift_selections: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let selection_mode: Bound<'py, PyAny> = if selection_mode.is_truthy()? {
        selection_mode.clone()
    } else {
        let mode_class = py.import(PLANNER_TYPES)?.getattr("DirectSelectionMode")?;
        mode_class.getattr(if selected_model_keys.is_truthy()? {
            "EXPLICIT"
        } else {
            "ALL_MODELS"
        })?
    };
    let project: Project<'py> = Project::new(py, graph, realized_project)?;
    let desired_state: Bound<'py, PyAny> = realized_project.getattr("desired_state")?;
    let mutable_ref_keys: Vec<Bound<'py, PyAny>> = desired_state
        .getattr("mutable_ref_warning_keys")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let mut source_drift: HashSet<usize> = HashSet::new();
    for selection in source_drift_selections.try_iter()? {
        source_drift.extend(project.indexes(&selection?.getattr("model_keys")?)?);
    }
    let nodes: Vec<NodeArgument> = project
        .keys
        .iter()
        .map(|key| project.node(py, graph, realized_project, key))
        .collect::<PyResult<_>>()?;
    let catalog: Vec<CatalogArgument> = snapshot
        .getattr("catalog")?
        .getattr("relations")?
        .try_iter()?
        .map(|relation| {
            let relation: Bound<'py, PyAny> = relation?;
            Ok((
                relation.getattr("name")?.extract()?,
                text(&relation.getattr("engine")?)? == CATALOG_MATERIALIZED_VIEW_ENGINE,
                relation
                    .getattr("columns")?
                    .try_iter()?
                    .map(|column| column?.getattr("name")?.extract())
                    .collect::<PyResult<_>>()?,
                relation.getattr("source_relation_names")?.extract()?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let external_columns: Vec<ExternalArgument> = desired_state
        .getattr("external_source_replay_configs")?
        .try_iter()?
        .map(|config| {
            let config: Bound<'py, PyAny> = config?;
            let column = |name: &str| truthy_text(&config.getattr(name)?);
            Ok((
                config.getattr("table_name")?.extract()?,
                [
                    column("partition_column_name")?,
                    column("offset_column_name")?,
                    column("timestamp_column_name")?,
                    column("landed_at_column_name")?,
                    column("cursor_column_name")?,
                ],
            ))
        })
        .collect::<PyResult<_>>()?;
    let fingerprints: Bound<'py, PyAny> = snapshot.getattr("fingerprints")?;
    let fingerprint_argument: FingerprintArgument = (
        text(&fingerprints.getattr("status")?)? != "unavailable",
        fingerprints.getattr("warning")?.extract()?,
        fingerprints
            .getattr("baselines")?
            .try_iter()?
            .map(|record| {
                let record: Bound<'py, PyAny> = record?;
                Ok((
                    record.getattr("logical_model_identity")?.extract()?,
                    record.getattr("definition_sql")?.extract()?,
                    record.getattr("definition_hash")?.extract()?,
                ))
            })
            .collect::<PyResult<_>>()?,
    );
    let mutable_ref_names: Vec<String> = mutable_ref_keys
        .iter()
        .map(|key| key.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    let result: DirectPlanResult = run_direct_plan(
        py,
        nodes,
        database,
        &text(&selection_mode)?,
        project.indexes(selected_model_keys)?,
        source_drift,
        include_missing_upstream,
        effective_start_time.is_some(),
        catalog,
        external_columns,
        fingerprint_argument,
        mutable_ref_names,
    )
    .map_err(|error| direct_plan_error(py, error))?;
    direct_plan(
        py,
        &project,
        &mutable_ref_keys,
        result,
        database,
        selection_mode,
        effective_start_time,
        source_drift_selections,
    )
}

/// A `ValueError` from the planner as `DirectPlanError`, raised `from None`.
fn direct_plan_error(py: Python<'_>, error: PyErr) -> PyErr {
    if !error.is_instance_of::<PyValueError>(py) {
        return error;
    }
    let raised = error
        .value(py)
        .str()
        .and_then(|message| {
            py.import(PLANNER_EXCEPTIONS)?
                .getattr("DirectPlanError")?
                .call1((message,))
        })
        .map(PyErr::from_value);
    match raised {
        Ok(failure) => {
            failure.set_cause(py, None);
            failure
        }
        Err(failure) => failure,
    }
}

#[allow(clippy::too_many_arguments)]
fn direct_plan<'py>(
    py: Python<'py>,
    project: &Project<'py>,
    mutable_ref_keys: &[Bound<'py, PyAny>],
    result: DirectPlanResult,
    database: &str,
    selection_mode: Bound<'py, PyAny>,
    effective_start_time: Option<String>,
    source_drift_selections: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let (
        user_scope,
        execution_scope,
        prerequisite_execution_scope,
        prerequisites,
        entries,
        replay_roots,
        teardown,
        creation,
        warnings,
    ) = result;
    let models: Bound<'py, PyModule> = py.import(PLANNER_MODELS)?;
    let key = |position: usize| project.keys[position].clone();
    let key_tuple = |positions: &[usize]| -> PyResult<Bound<'py, PyAny>> {
        Ok(PyTuple::new(py, positions.iter().map(|position| key(*position)))?.into_any())
    };
    let prerequisite_class = models.getattr("DirectPrerequisite")?;
    let prerequisite_scope: Vec<Bound<'py, PyAny>> = prerequisites
        .into_iter()
        .map(|(position, relation_name, present, framework_managed)| {
            keyword_call(
                &prerequisite_class,
                &[
                    ("key", key(position)),
                    (
                        "relation_names",
                        PyTuple::new(py, [relation_name])?.into_any(),
                    ),
                    ("present", present.into_bound_py_any(py)?),
                    (
                        "framework_managed",
                        framework_managed.into_bound_py_any(py)?,
                    ),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let entry_class = models.getattr("DirectPlanEntry")?;
    let sql_change_class = models.getattr("DirectSqlChange")?;
    let direct_entries: Vec<Bound<'py, PyAny>> = entries
        .into_iter()
        .map(
            |(position, reason, relations, driving_input, is_replay_root, sql_change)| {
                let (status, current_hash, previous_sql, previous_hash, unified_diff, warning) =
                    sql_change;
                let model_key: Bound<'py, PyAny> = key(position);
                let current_sql: Bound<'py, PyAny> = project
                    .model_by_key
                    .get_item(&model_key)?
                    .ok_or_else(|| PyKeyError::new_err(model_key.clone().unbind()))?
                    .getattr("query")?;
                keyword_call(
                    &entry_class,
                    &[
                        ("model_key", model_key),
                        ("reason", reason.into_bound_py_any(py)?),
                        (
                            "relation_names",
                            PyTuple::new(py, relations.iter().map(|(name, _)| name))?.into_any(),
                        ),
                        (
                            "resource_kinds",
                            PyTuple::new(py, relations.iter().map(|(_, kind)| *kind))?.into_any(),
                        ),
                        (
                            "driving_input_key",
                            driving_input.map_or_else(|| py.None().into_bound(py), key),
                        ),
                        ("is_replay_root", is_replay_root.into_bound_py_any(py)?),
                        (
                            "sql_change",
                            keyword_call(
                                &sql_change_class,
                                &[
                                    ("status", status.into_bound_py_any(py)?),
                                    ("current_sql", current_sql),
                                    ("current_hash", current_hash.into_bound_py_any(py)?),
                                    ("previous_sql", previous_sql.into_bound_py_any(py)?),
                                    ("previous_hash", previous_hash.into_bound_py_any(py)?),
                                    ("unified_diff", unified_diff.into_bound_py_any(py)?),
                                    ("warning", warning.into_bound_py_any(py)?),
                                ],
                            )?,
                        ),
                    ],
                )
            },
        )
        .collect::<PyResult<_>>()?;
    let root_class = models.getattr("DirectReplayRoot")?;
    let columns_class = py.import(ADAPTER_MODELS)?.getattr("AdapterReplayColumns")?;
    let roots: Vec<Bound<'py, PyAny>> = replay_roots
        .into_iter()
        .map(
            |(
                position,
                driving_input,
                relation_name,
                columns,
                lineage,
                propagated,
                aggregate,
                settings,
            )| {
                let [partition, offset, timestamp, landed_at, cursor] = columns;
                keyword_call(
                    &root_class,
                    &[
                        ("model_key", key(position)),
                        ("driving_input_key", key(driving_input)),
                        (
                            "driving_input_relation_name",
                            relation_name.into_bound_py_any(py)?,
                        ),
                        (
                            "driving_input_replay_columns",
                            keyword_call(
                                &columns_class,
                                &[
                                    ("partition", partition.into_bound_py_any(py)?),
                                    ("offset", offset.into_bound_py_any(py)?),
                                    ("timestamp", timestamp.into_bound_py_any(py)?),
                                    ("landed_at", landed_at.into_bound_py_any(py)?),
                                    ("cursor", cursor.into_bound_py_any(py)?),
                                ],
                            )?,
                        ),
                        ("replay_boundary_mode", lineage.into_bound_py_any(py)?),
                        ("propagated_model_keys", key_tuple(&propagated)?),
                        ("has_aggregate_semantics", aggregate.into_bound_py_any(py)?),
                        (
                            "replay_settings",
                            PyTuple::new(
                                py,
                                settings
                                    .into_iter()
                                    .map(|setting| setting.into_pyobject(py))
                                    .collect::<PyResult<Vec<_>>>()?,
                            )?
                            .into_any(),
                        ),
                    ],
                )
            },
        )
        .collect::<PyResult<_>>()?;
    let operation_class = models.getattr("DirectRelationOperation")?;
    let operations = |operations: Vec<(String, usize, &'static str)>,
                      action: &str|
     -> PyResult<Bound<'py, PyAny>> {
        let built: Vec<Bound<'py, PyAny>> = operations
            .into_iter()
            .map(|(relation_name, position, kind)| {
                keyword_call(
                    &operation_class,
                    &[
                        ("relation_name", relation_name.into_bound_py_any(py)?),
                        ("action", action.into_bound_py_any(py)?),
                        ("model_key", key(position)),
                        ("resource_kind", kind.into_bound_py_any(py)?),
                    ],
                )
            })
            .collect::<PyResult<_>>()?;
        Ok(PyTuple::new(py, built)?.into_any())
    };
    let warning_class = models.getattr("PlannerWarning")?;
    let planner_warnings: Vec<Bound<'py, PyAny>> = warnings
        .into_iter()
        .map(|position| {
            let warning_key: Bound<'py, PyAny> = mutable_ref_keys
                .get(position)
                .cloned()
                .ok_or_else(|| PyIndexError::new_err("tuple index out of range"))?;
            keyword_call(
                &warning_class,
                &[
                    (
                        "warning_code",
                        MUTABLE_REF_WARNING_CODE.into_bound_py_any(py)?,
                    ),
                    (
                        "message",
                        MUTABLE_REF_WARNING_MESSAGE.into_bound_py_any(py)?,
                    ),
                    ("root_key", warning_key.clone()),
                    ("target_key", warning_key),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    keyword_call(
        &models.getattr("DirectPlan")?,
        &[
            ("database", database.into_bound_py_any(py)?),
            ("selection_mode", selection_mode),
            (
                "effective_start_time",
                effective_start_time.into_bound_py_any(py)?,
            ),
            ("user_scope", key_tuple(&user_scope)?),
            ("execution_scope", key_tuple(&execution_scope)?),
            (
                "prerequisite_execution_scope",
                key_tuple(&prerequisite_execution_scope)?,
            ),
            (
                "prerequisite_scope",
                PyTuple::new(py, prerequisite_scope)?.into_any(),
            ),
            ("entries", PyTuple::new(py, direct_entries)?.into_any()),
            ("replay_roots", PyTuple::new(py, roots)?.into_any()),
            ("teardown_operations", operations(teardown, "drop")?),
            ("creation_operations", operations(creation, "create")?),
            ("warnings", PyTuple::new(py, planner_warnings)?.into_any()),
            ("source_drift_selections", source_drift_selections.clone()),
        ],
    )
}

/// Actual materialized views outside `execution_scope` that read a relation the prerequisite
/// replay rebuilds, sorted by name.
#[pyfunction]
#[pyo3(signature = (*, realized_project, catalog, prerequisite_execution_scope, execution_scope))]
fn find_direct_out_of_scope_consumers<'py>(
    py: Python<'py>,
    realized_project: &Bound<'_, PyAny>,
    catalog: &Bound<'_, PyAny>,
    prerequisite_execution_scope: &Bound<'_, PyAny>,
    execution_scope: &Bound<'_, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let relation_names: Bound<'_, PyAny> =
        realized_project.getattr("relation_name_by_logical_key")?;
    let rebuilt: HashSet<String> = prerequisite_execution_scope
        .try_iter()?
        .map(|key| relation_names.get_item(key?)?.extract())
        .collect::<PyResult<_>>()?;
    if rebuilt.is_empty() {
        return Ok(PyTuple::empty(py));
    }
    let materialized_view_class = py
        .import(ADAPTER_MODELS)?
        .getattr("AdapterMaterializedView")?;
    let resources_by_key: Bound<'_, PyAny> =
        realized_project.getattr("resources_by_logical_key")?;
    let mut in_scope_views: HashSet<String> = HashSet::new();
    for key in execution_scope.try_iter()? {
        for resource in resources_by_key
            .call_method1("get", (key?, PyTuple::empty(py)))?
            .try_iter()?
        {
            let resource: Bound<'_, PyAny> = resource?;
            if resource.is_instance(&materialized_view_class)? {
                in_scope_views.insert(resource.getattr("name")?.extract()?);
            }
        }
    }
    let mut consumers: Vec<String> = Vec::new();
    for relation in catalog.getattr("relations")?.try_iter()? {
        let relation: Bound<'_, PyAny> = relation?;
        if text(&relation.getattr("engine")?)? != CATALOG_MATERIALIZED_VIEW_ENGINE {
            continue;
        }
        let name: String = relation.getattr("name")?.extract()?;
        if in_scope_views.contains(&name) {
            continue;
        }
        let sources: Vec<String> = relation.getattr("source_relation_names")?.extract()?;
        if sources.iter().any(|source| rebuilt.contains(source)) {
            consumers.push(name);
        }
    }
    consumers.sort_unstable();
    PyTuple::new(py, consumers)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_direct_build, module)?)?;
    module.add_function(wrap_pyfunction!(
        find_direct_out_of_scope_consumers,
        module
    )?)
}
