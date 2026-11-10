//! Planner support around the native planners: adapter resource requests, staged shadow
//! resources, replay queries, metadata state records, warehouse snapshot loading, root
//! inspection, physical names and direct model fingerprints. Warehouse reads still go through
//! the Python adapter connection.

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use sqltide_planner::direct::sha256_hex;

use crate::artifacts::json_dumps;
use crate::model_sql::chained;

const NATIVE: &str = "sqltide._native";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const COMPILE_TYPES: &str = "sqltide.compiler.compile.types";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const SQL_ANALYSIS_MODELS: &str = "sqltide.compiler.sql_analysis.models";
const SQL_ANALYSIS_EXCEPTIONS: &str = "sqltide.compiler.sql_analysis.exceptions";
const DEPLOYMENT_SEPARATOR: &str = "__";

type Arguments<'a, 'py> = &'a [(&'a str, Bound<'py, PyAny>)];

fn native<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(NATIVE)?.getattr(name)
}

fn none(py: Python<'_>) -> Bound<'_, PyAny> {
    py.None().into_bound(py)
}

fn call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: Arguments<'_, 'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(callable.py());
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

fn build<'py>(
    py: Python<'py>,
    module: &str,
    class: &str,
    arguments: Arguments<'_, 'py>,
) -> PyResult<Bound<'py, PyAny>> {
    call(&py.import(module)?.getattr(class)?, arguments)
}

fn replace<'py>(
    value: &Bound<'py, PyAny>,
    changes: Arguments<'_, 'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(value.py());
    for (name, change) in changes {
        keywords.set_item(name, change)?;
    }
    value
        .py()
        .import("dataclasses")?
        .getattr("replace")?
        .call((value,), Some(&keywords))
}

fn items<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    value.try_iter()?.collect()
}

fn tuple<'py>(py: Python<'py>, values: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn object<'py, T: IntoPyObjectExt<'py>>(py: Python<'py>, value: T) -> PyResult<Bound<'py, PyAny>> {
    value.into_bound_py_any(py)
}

fn is_a(value: &Bound<'_, PyAny>, module: &str, class: &str) -> PyResult<bool> {
    value.is_instance(&value.py().import(module)?.getattr(class)?)
}

/// `sorted(values, key=key)`: Python's stable sort over keys evaluated once each.
fn sorted_by<'py>(
    py: Python<'py>,
    values: Vec<Bound<'py, PyAny>>,
    key: impl Fn(&Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let decorated: Vec<Bound<'py, PyAny>> = values
        .iter()
        .enumerate()
        .map(|(index, value)| tuple(py, vec![key(value)?, object(py, index)?]))
        .collect::<PyResult<_>>()?;
    let ordered: Bound<'py, PyAny> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((PyList::new(py, decorated)?,))?;
    items(&ordered)?
        .iter()
        .map(|entry| Ok(values[entry.get_item(1)?.extract::<usize>()?].clone()))
        .collect()
}

/// `tuple(sorted((mapping or {}).items()))`.
fn sorted_settings<'py>(mapping: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = mapping.py();
    let pairs: Bound<'py, PyAny> = if mapping.is_truthy()? {
        mapping.call_method0("items")?
    } else {
        PyDict::new(py).call_method0("items")?
    };
    let ordered: Bound<'py, PyAny> = py.import("builtins")?.getattr("sorted")?.call1((pairs,))?;
    py.get_type::<PyTuple>().call1((ordered,))
}

fn plan_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import(PLANNER_EXCEPTIONS)?
            .getattr("DeploymentPlanError")?
            .call1((message,))?,
    ))
}

fn database_template<'py>(desired_object: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let template: Bound<'py, PyAny> = desired_object
        .getattr("spec")?
        .getattr("database_template")?;
    if template.is_none() {
        return Err(plan_error(
            desired_object.py(),
            format!(
                "View '{}' lacks an analyzed database template",
                desired_object.getattr("name")?.str()?
            ),
        )?);
    }
    Ok(template)
}

/// The neutral adapter resource request of one desired object.
#[pyfunction]
pub(crate) fn build_adapter_resource<'py>(
    py: Python<'py>,
    desired_object: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let kafka: bool = is_a(desired_object, COMPILE_MODELS, "DesiredKafkaTable")?;
    let table: bool = is_a(desired_object, COMPILE_MODELS, "DesiredTable")?;
    let columns: Bound<'py, PyAny> = if kafka || table {
        let column_class: Bound<'py, PyAny> =
            py.import(ADAPTER_MODELS)?.getattr("AdapterColumn")?;
        let columns: Vec<Bound<'py, PyAny>> =
            items(&desired_object.getattr("spec")?.getattr("columns")?)?
                .iter()
                .map(|column| {
                    call(
                        &column_class,
                        &[
                            ("name", column.getattr("name")?),
                            ("type", column.getattr("type")?),
                            ("default_expression", column.getattr("default")?),
                        ],
                    )
                })
                .collect::<PyResult<_>>()?;
        tuple(py, columns)?
    } else {
        PyTuple::empty(py).into_any()
    };
    if kafka {
        let source: Bound<'py, PyAny> = desired_object.getattr("kafka")?;
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterManagedSource",
            &[
                (
                    "source_kind",
                    py.import(ADAPTER_CONSTANTS)?
                        .getattr("MANAGED_SOURCE_KIND_KAFKA")?,
                ),
                ("name", desired_object.getattr("name")?),
                ("columns", columns),
                ("broker_list", source.getattr("broker_list")?),
                ("topic", source.getattr("topic")?),
                ("consumer_group", source.getattr("consumer_group")?),
                ("format", source.getattr("format")?),
                ("settings", sorted_settings(&source.getattr("settings")?)?),
                (
                    "naming_macro_fingerprint",
                    desired_object
                        .getattr("spec")?
                        .getattr("naming_macro_fingerprint")?,
                ),
            ],
        );
    }
    if table {
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterTable",
            &[
                ("name", desired_object.getattr("name")?),
                ("columns", columns),
                ("engine", desired_object.getattr("engine")?),
                ("order_by", desired_object.getattr("order_by")?),
                ("partition_by", desired_object.getattr("partition_by")?),
                ("ttl", desired_object.getattr("ttl")?),
                (
                    "settings",
                    sorted_settings(&desired_object.getattr("settings")?)?,
                ),
            ],
        );
    }
    if is_a(desired_object, COMPILE_MODELS, "DesiredMaterializedView")? {
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterMaterializedView",
            &[
                ("name", desired_object.getattr("name")?),
                (
                    "source_relation_name",
                    desired_object.getattr("source_table_name")?,
                ),
                (
                    "target_relation_name",
                    desired_object.getattr("target_table_name")?,
                ),
                ("query", desired_object.getattr("query")?),
                ("database_template", database_template(desired_object)?),
                ("refresh", desired_object.getattr("refresh")?),
                ("append", desired_object.getattr("append")?),
                ("dead_letters", desired_object.getattr("dead_letters")?),
            ],
        );
    }
    build(
        py,
        ADAPTER_MODELS,
        "AdapterView",
        &[
            ("name", desired_object.getattr("name")?),
            ("query", desired_object.getattr("query")?),
            ("database_template", database_template(desired_object)?),
        ],
    )
}

/// Relation rewrites moving each staged logical table onto its physical name, in either its
/// unqualified or placeholder-qualified form.
fn shadow_relation_rewrites<'py>(
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = physical_name_by_key.py();
    let table_type: Bound<'py, PyAny> = py
        .import(COMPILE_CONSTANTS)?
        .getattr("DESIRED_OBJECT_TYPE_TABLE")?;
    let placeholder: Bound<'py, PyAny> = py
        .import(ADAPTER_CONSTANTS)?
        .getattr("ADAPTER_DATABASE_PLACEHOLDER")?;
    let rewrite_class: Bound<'py, PyAny> = py
        .import(SQL_ANALYSIS_MODELS)?
        .getattr("SqlRelationRewrite")?;
    let mut rewrites: Vec<Bound<'py, PyAny>> = Vec::new();
    for entry in items(&physical_name_by_key.call_method0("items")?)? {
        let (key, physical_name): (Bound<'py, PyAny>, Bound<'py, PyAny>) = entry.extract()?;
        if !key.getattr("object_type")?.eq(&table_type)? {
            continue;
        }
        rewrites.push(call(
            &rewrite_class,
            &[
                ("source_name", key.getattr("name")?),
                ("target_relation", physical_name),
                (
                    "source_databases",
                    tuple(py, vec![none(py), placeholder.clone()])?,
                ),
                ("preserve_source_database", object(py, true)?),
            ],
        )?);
    }
    tuple(py, rewrites)
}

fn rewrite_shadow_query<'py>(
    query: &Bound<'py, PyAny>,
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = query.py();
    call(
        &native(py, "rewrite_query")?,
        &[
            ("sql", query.clone()),
            ("dialect", object(py, "clickhouse")?),
            (
                "relation_rewrites",
                shadow_relation_rewrites(physical_name_by_key)?,
            ),
        ],
    )?
    .getattr("query")
}

fn rewrite_shadow_template<'py>(
    template: &Bound<'py, PyAny>,
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = template.py();
    call(
        &native(py, "rewrite_template_query")?,
        &[
            ("template", template.clone()),
            ("dialect", object(py, "clickhouse")?),
            (
                "database_placeholder",
                py.import(ADAPTER_CONSTANTS)?
                    .getattr("ADAPTER_DATABASE_PLACEHOLDER")?,
            ),
            (
                "relation_rewrites",
                shadow_relation_rewrites(physical_name_by_key)?,
            ),
        ],
    )?
    .getattr("query")
}

/// The staged view's database template: its authored template rewritten byte-preservingly, or
/// the rewritten canonical query when it has none.
fn shadow_template<'py>(
    desired_object: &Bound<'py, PyAny>,
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let template: Bound<'py, PyAny> = desired_object
        .getattr("spec")?
        .getattr("database_template")?;
    if template.is_none() {
        rewrite_shadow_query(&desired_object.getattr("query")?, physical_name_by_key)
    } else {
        rewrite_shadow_template(&template, physical_name_by_key)
    }
}

fn physical_table_name<'py>(
    logical_name: Bound<'py, PyAny>,
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let table_type: Bound<'py, PyAny> = logical_name
        .py()
        .import(COMPILE_CONSTANTS)?
        .getattr("DESIRED_OBJECT_TYPE_TABLE")?;
    for entry in items(&physical_name_by_key.call_method0("items")?)? {
        let (key, physical_name): (Bound<'py, PyAny>, Bound<'py, PyAny>) = entry.extract()?;
        if key.getattr("object_type")?.eq(&table_type)? && key.getattr("name")?.eq(&logical_name)? {
            return Ok(physical_name);
        }
    }
    Ok(logical_name)
}

/// A staged physical resource, with its logical table references rewritten onto staged names.
#[pyfunction]
#[pyo3(signature = (*, desired_object, physical_name, physical_name_by_key))]
pub(crate) fn build_shadow_adapter_resource<'py>(
    py: Python<'py>,
    desired_object: &Bound<'py, PyAny>,
    physical_name: &Bound<'py, PyAny>,
    physical_name_by_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let key: Bound<'py, PyAny> = replace(
        &desired_object.getattr("key")?,
        &[("name", physical_name.clone())],
    )?;
    if is_a(desired_object, COMPILE_MODELS, "DesiredTable")? {
        return build_adapter_resource(py, &replace(desired_object, &[("key", key)])?);
    }
    if is_a(desired_object, COMPILE_MODELS, "DesiredView")? {
        let query: Bound<'py, PyAny> =
            rewrite_shadow_query(&desired_object.getattr("query")?, physical_name_by_key)?;
        let template: Bound<'py, PyAny> = shadow_template(desired_object, physical_name_by_key)?;
        let spec: Bound<'py, PyAny> = build(
            py,
            COMPILE_MODELS,
            "ViewSpec",
            &[("query", query), ("database_template", template)],
        )?;
        return build_adapter_resource(
            py,
            &replace(desired_object, &[("key", key), ("spec", spec)])?,
        );
    }
    let source_name: Bound<'py, PyAny> = physical_table_name(
        desired_object.getattr("source_table_name")?,
        physical_name_by_key,
    )?;
    let target_name: Bound<'py, PyAny> = physical_table_name(
        desired_object.getattr("target_table_name")?,
        physical_name_by_key,
    )?;
    let query: Bound<'py, PyAny> =
        rewrite_shadow_query(&desired_object.getattr("query")?, physical_name_by_key)?;
    let template: Bound<'py, PyAny> = shadow_template(desired_object, physical_name_by_key)?;
    let spec: Bound<'py, PyAny> = build(
        py,
        COMPILE_MODELS,
        "MaterializedViewSpec",
        &[
            ("source_table_name", source_name),
            ("target_table_name", target_name),
            ("query", query),
            ("database_template", template),
        ],
    )?;
    build_adapter_resource(
        py,
        &replace(desired_object, &[("key", key), ("spec", spec)])?,
    )
}

/// A replay query with side references rewritten onto physical relations and its aggregate
/// semantics classified.
#[pyfunction]
#[pyo3(signature = (
    *, query, database_template = None, source_relation_name, database, physical_relation_mappings
))]
pub(crate) fn build_adapter_replay_query<'py>(
    py: Python<'py>,
    query: &Bound<'py, PyAny>,
    database_template: Option<Bound<'py, PyAny>>,
    source_relation_name: &Bound<'py, PyAny>,
    database: &Bound<'py, PyAny>,
    physical_relation_mappings: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let rewrite_class: Bound<'py, PyAny> = py
        .import(SQL_ANALYSIS_MODELS)?
        .getattr("SqlRelationRewrite")?;
    let mut rewrites: Vec<Bound<'py, PyAny>> = Vec::new();
    for mapping in items(physical_relation_mappings)? {
        let logical_name: Bound<'py, PyAny> = mapping.getattr("logical_name")?;
        if logical_name.eq(source_relation_name)? {
            continue;
        }
        let target: String = format!(
            "{}.{}",
            database.str()?,
            mapping.getattr("physical_name")?.str()?
        );
        rewrites.push(call(
            &rewrite_class,
            &[
                ("source_name", logical_name),
                ("target_relation", object(py, target)?),
            ],
        )?);
    }
    let rewrites: Bound<'py, PyAny> = tuple(py, rewrites)?;
    let result: PyResult<Bound<'py, PyAny>> = match database_template {
        Some(template) if !template.is_none() => call(
            &native(py, "rewrite_template_query")?,
            &[
                ("template", template),
                ("dialect", object(py, "clickhouse")?),
                (
                    "database_placeholder",
                    py.import(ADAPTER_CONSTANTS)?
                        .getattr("ADAPTER_DATABASE_PLACEHOLDER")?,
                ),
                ("relation_rewrites", rewrites),
            ],
        ),
        _ => call(
            &native(py, "rewrite_query")?,
            &[
                ("sql", query.clone()),
                ("dialect", object(py, "clickhouse")?),
                ("relation_rewrites", rewrites),
            ],
        ),
    };
    let analysis_error: Bound<'py, PyAny> = py
        .import(SQL_ANALYSIS_EXCEPTIONS)?
        .getattr("SqlAnalysisError")?;
    let result: Bound<'py, PyAny> = match result {
        Ok(result) => result,
        Err(failure) if failure.value(py).is_instance(&analysis_error)? => {
            let context: Bound<'py, PyAny> = failure.value(py).clone().into_any();
            let error: Bound<'py, PyAny> = py
                .import(PLANNER_EXCEPTIONS)?
                .getattr("DeploymentPlanError")?
                .call1((context.str()?,))?;
            chained(&error, &context, None)?;
            return Err(PyErr::from_value(error));
        }
        Err(failure) => return Err(failure),
    };
    build(
        py,
        ADAPTER_MODELS,
        "AdapterReplayQuery",
        &[
            ("query", result.getattr("query")?),
            (
                "physical_relation_mappings",
                physical_relation_mappings.clone(),
            ),
            (
                "aggregate_semantics",
                result.getattr("has_aggregate_semantics")?,
            ),
        ],
    )
}

fn key_order<'py>(key: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    Ok(vec![key.getattr("object_type")?, key.getattr("name")?])
}

fn normalized_deployment<'py>(deployment: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = deployment.py();
    let root_keys: Vec<Bound<'py, PyAny>> = sorted_by(
        py,
        items(&deployment.getattr("selected_root_keys")?)?,
        |key| tuple(py, key_order(key)?),
    )?;
    let warning_codes: Vec<Bound<'py, PyAny>> =
        sorted_by(py, items(&deployment.getattr("warning_codes")?)?, |code| {
            Ok(code.clone())
        })?;
    let mappings: Vec<Bound<'py, PyAny>> = sorted_by(
        py,
        items(&deployment.getattr("prepared_object_mappings")?)?,
        |mapping| tuple(py, key_order(&mapping.getattr("logical_key")?)?),
    )?;
    let mut arguments: Vec<(&str, Bound<'py, PyAny>)> = Vec::new();
    for field in [
        "deployment_id",
        "created_at",
        "status",
        "replay_lineage_mode",
    ] {
        arguments.push((field, deployment.getattr(field)?));
    }
    arguments.push(("selected_root_keys", tuple(py, root_keys)?));
    arguments.push(("warning_codes", tuple(py, warning_codes)?));
    arguments.push(("prepared_object_mappings", tuple(py, mappings)?));
    for field in ["workflow_fingerprint", "boundary_time", "tool_version"] {
        arguments.push((field, deployment.getattr(field)?));
    }
    build(py, PLANNER_MODELS, "DeploymentRecord", &arguments)
}

/// Deterministically ordered metadata state records.
#[pyfunction]
#[pyo3(signature = (*, object_states, deployments, deployment_watermarks, publish_events))]
pub(crate) fn build_metadata_state<'py>(
    py: Python<'py>,
    object_states: &Bound<'py, PyAny>,
    deployments: &Bound<'py, PyAny>,
    deployment_watermarks: &Bound<'py, PyAny>,
    publish_events: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let object_states: Vec<Bound<'py, PyAny>> = sorted_by(py, items(object_states)?, |state| {
        let key: Bound<'py, PyAny> = state.getattr("key")?;
        let mut order: Vec<Bound<'py, PyAny>> = vec![state.getattr("deployment_id")?];
        order.extend(key_order(&key)?);
        tuple(py, order)
    })?;
    let normalized: Vec<Bound<'py, PyAny>> = items(deployments)?
        .iter()
        .map(normalized_deployment)
        .collect::<PyResult<_>>()?;
    let deployments: Vec<Bound<'py, PyAny>> = sorted_by(py, normalized, |deployment| {
        deployment.getattr("deployment_id")
    })?;
    let watermarks: Vec<Bound<'py, PyAny>> =
        sorted_by(py, items(deployment_watermarks)?, |watermark| {
            let mut order: Vec<Bound<'py, PyAny>> = vec![watermark.getattr("deployment_id")?];
            order.extend(key_order(&watermark.getattr("root_key")?)?);
            order.push(watermark.getattr("boundary_key")?);
            tuple(py, order)
        })?;
    let event_class: Bound<'py, PyAny> =
        py.import(PLANNER_MODELS)?.getattr("PublishEventRecord")?;
    let events: Vec<Bound<'py, PyAny>> = items(publish_events)?
        .iter()
        .map(|event| {
            let names: Vec<Bound<'py, PyAny>> =
                sorted_by(py, items(&event.getattr("logical_view_names")?)?, |name| {
                    Ok(name.clone())
                })?;
            call(
                &event_class,
                &[
                    ("deployment_id", event.getattr("deployment_id")?),
                    ("published_at", event.getattr("published_at")?),
                    ("logical_view_names", tuple(py, names)?),
                    ("database", event.getattr("database")?),
                    (
                        "physical_relation_names",
                        event.getattr("physical_relation_names")?,
                    ),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let events: Vec<Bound<'py, PyAny>> = sorted_by(py, events, |event| {
        tuple(
            py,
            vec![
                event.getattr("published_at")?,
                event.getattr("deployment_id")?,
            ],
        )
    })?;
    build(
        py,
        PLANNER_MODELS,
        "MetadataState",
        &[
            ("object_states", tuple(py, object_states)?),
            ("deployments", tuple(py, deployments)?),
            ("deployment_watermarks", tuple(py, watermarks)?),
            ("publish_events", tuple(py, events)?),
        ],
    )
}

fn adapter_key<'py>(key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = key.py();
    build(
        py,
        ADAPTER_MODELS,
        "AdapterMetadataObjectKey",
        &[
            ("database", key.getattr("database")?),
            ("object_type", key.getattr("object_type")?.str()?.into_any()),
            ("name", key.getattr("name")?),
        ],
    )
}

fn adapter_deployment<'py>(deployment: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = deployment.py();
    let root_keys: Vec<Bound<'py, PyAny>> = items(&deployment.getattr("selected_root_keys")?)?
        .iter()
        .map(adapter_key)
        .collect::<PyResult<_>>()?;
    let mapping_class: Bound<'py, PyAny> = py
        .import(ADAPTER_MODELS)?
        .getattr("AdapterPreparedObjectMapping")?;
    let mappings: Vec<Bound<'py, PyAny>> = items(&deployment.getattr("prepared_object_mappings")?)?
        .iter()
        .map(|mapping| {
            call(
                &mapping_class,
                &[
                    (
                        "logical_key",
                        adapter_key(&mapping.getattr("logical_key")?)?,
                    ),
                    ("physical_name", mapping.getattr("physical_name")?),
                    ("logical_model_name", mapping.getattr("logical_model_name")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterDeploymentRecord",
        &[
            ("deployment_id", deployment.getattr("deployment_id")?),
            ("created_at", deployment.getattr("created_at")?),
            ("status", deployment.getattr("status")?),
            (
                "replay_lineage_mode",
                deployment.getattr("replay_lineage_mode")?.str()?.into_any(),
            ),
            ("selected_root_keys", tuple(py, root_keys)?),
            ("warning_codes", deployment.getattr("warning_codes")?),
            ("prepared_object_mappings", tuple(py, mappings)?),
            (
                "workflow_fingerprint",
                deployment.getattr("workflow_fingerprint")?,
            ),
            ("boundary_time", deployment.getattr("boundary_time")?),
            ("tool_version", deployment.getattr("tool_version")?),
        ],
    )
}

fn adapter_publish_event<'py>(event: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = event.py();
    let binding_class: Bound<'py, PyAny> =
        py.import(ADAPTER_MODELS)?.getattr("AdapterStableBinding")?;
    let database: Bound<'py, PyAny> = event.getattr("database")?;
    let logical_names: Bound<'py, PyAny> = event.getattr("logical_view_names")?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("strict", true)?;
    let pairs: Bound<'py, PyAny> = py.import("builtins")?.getattr("zip")?.call(
        (&logical_names, event.getattr("physical_relation_names")?),
        Some(&keywords),
    )?;
    let mut bindings: Vec<Bound<'py, PyAny>> = Vec::new();
    for pair in pairs.try_iter()? {
        let (logical_name, physical_name): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
            pair?.extract()?;
        bindings.push(call(
            &binding_class,
            &[
                ("database", database.clone()),
                ("logical_name", logical_name),
                ("physical_name", physical_name),
            ],
        )?);
    }
    build(
        py,
        ADAPTER_MODELS,
        "AdapterPublishEventRecord",
        &[
            ("deployment_id", event.getattr("deployment_id")?),
            ("published_at", event.getattr("published_at")?),
            ("logical_view_names", logical_names),
            ("bindings", tuple(py, bindings)?),
        ],
    )
}

/// Logical metadata records converted into the neutral persistence contract.
#[pyfunction]
pub(crate) fn build_adapter_metadata_state<'py>(
    py: Python<'py>,
    state: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let deployments: Vec<Bound<'py, PyAny>> = items(&state.getattr("deployments")?)?;
    let deployment_by_id: Bound<'py, PyDict> = PyDict::new(py);
    for deployment in &deployments {
        deployment_by_id.set_item(deployment.getattr("deployment_id")?, deployment)?;
    }
    let mapping_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for deployment in &deployments {
        let deployment_id: Bound<'py, PyAny> = deployment.getattr("deployment_id")?;
        for mapping in items(&deployment.getattr("prepared_object_mappings")?)? {
            mapping_by_key.set_item(
                (deployment_id.clone(), mapping.getattr("logical_key")?),
                mapping,
            )?;
        }
    }
    let reconcile_kind: Bound<'py, PyAny> = py
        .import(ADAPTER_CONSTANTS)?
        .getattr("VIRTUAL_OBJECT_STATE_KIND_RECONCILE")?;
    let record_class: Bound<'py, PyAny> = py
        .import(ADAPTER_MODELS)?
        .getattr("AdapterObjectStateRecord")?;
    let mut object_states: Vec<Bound<'py, PyAny>> = Vec::new();
    for record in items(&state.getattr("object_states")?)? {
        let deployment_id: Bound<'py, PyAny> = record.getattr("deployment_id")?;
        let key: Bound<'py, PyAny> = record.getattr("key")?;
        let mapping: Option<Bound<'py, PyAny>> =
            mapping_by_key.get_item((deployment_id.clone(), key.clone()))?;
        let state_kind: Bound<'py, PyAny> = record.getattr("state_kind")?;
        let physical_relation_name: Bound<'py, PyAny> = match &mapping {
            Some(mapping) => mapping.getattr("physical_name")?,
            None if state_kind.eq(&reconcile_kind)? => key.getattr("name")?,
            None => none(py),
        };
        let logical_model_name: Bound<'py, PyAny> = match &mapping {
            Some(mapping) => mapping.getattr("logical_model_name")?,
            None => key.getattr("name")?,
        };
        let is_selected_root: bool = match deployment_by_id.get_item(&deployment_id)? {
            Some(deployment) => deployment.getattr("selected_root_keys")?.contains(&key)?,
            None => false,
        };
        object_states.push(call(
            &record_class,
            &[
                ("deployment_id", deployment_id),
                ("key", adapter_key(&key)?),
                (
                    "normalized_fingerprint",
                    record.getattr("normalized_fingerprint")?,
                ),
                ("normalized_query", record.getattr("normalized_query")?),
                ("recorded_at", record.getattr("recorded_at")?),
                ("observation_id", record.getattr("observation_id")?),
                ("state_kind", state_kind),
                ("physical_database_name", key.getattr("database")?),
                ("physical_relation_name", physical_relation_name),
                ("logical_model_database", key.getattr("database")?),
                ("logical_model_name", logical_model_name),
                ("is_selected_root", object(py, is_selected_root)?),
            ],
        )?);
    }
    let deployment_records: Vec<Bound<'py, PyAny>> = deployments
        .iter()
        .map(adapter_deployment)
        .collect::<PyResult<_>>()?;
    let watermark_class: Bound<'py, PyAny> = py
        .import(ADAPTER_MODELS)?
        .getattr("AdapterDeploymentWatermarkRecord")?;
    let watermarks: Vec<Bound<'py, PyAny>> = items(&state.getattr("deployment_watermarks")?)?
        .iter()
        .map(|record| {
            call(
                &watermark_class,
                &[
                    ("deployment_id", record.getattr("deployment_id")?),
                    ("root_key", adapter_key(&record.getattr("root_key")?)?),
                    ("anchor_key", adapter_key(&record.getattr("anchor_key")?)?),
                    ("boundary_key", record.getattr("boundary_key")?),
                    ("cutoff_value", record.getattr("cutoff_value")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let events: Vec<Bound<'py, PyAny>> = items(&state.getattr("publish_events")?)?
        .iter()
        .map(adapter_publish_event)
        .collect::<PyResult<_>>()?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterMetadataState",
        &[
            ("object_states", tuple(py, object_states)?),
            ("deployments", tuple(py, deployment_records)?),
            ("deployment_watermarks", tuple(py, watermarks)?),
            ("publish_events", tuple(py, events)?),
        ],
    )
}

fn optional_text<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        Ok(value)
    } else {
        Ok(value.str()?.into_any())
    }
}

/// One persisted object state row decoded from a warehouse result mapping.
#[pyfunction]
fn decode_object_state_metadata_row<'py>(
    py: Python<'py>,
    row: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let text =
        |name: &str| -> PyResult<Bound<'py, PyAny>> { Ok(row.get_item(name)?.str()?.into_any()) };
    build(
        py,
        PLANNER_MODELS,
        "ObjectStateMetadataRow",
        &[
            ("deployment_id", text("deployment_id")?),
            (
                "database_name",
                optional_text(row.get_item("database_name")?)?,
            ),
            (
                "object_type",
                py.import(COMPILE_TYPES)?
                    .getattr("DesiredObjectType")?
                    .call1((text("object_type")?,))?,
            ),
            ("object_name", text("object_name")?),
            ("normalized_fingerprint", text("normalized_fingerprint")?),
            (
                "normalized_query",
                optional_text(row.get_item("normalized_query")?)?,
            ),
            ("recorded_at", text("recorded_at")?),
            ("observation_id", text("observation_id")?),
            ("state_kind", text("state_kind")?),
        ],
    )
}

fn capability_error(client: &Bound<'_, PyAny>, unsupported: &str) -> PyResult<PyErr> {
    let py: Python<'_> = client.py();
    Ok(PyErr::from_value(
        py.import(ADAPTER_EXCEPTIONS)?
            .getattr("AdapterCapabilityError")?
            .call1((format!(
                "Adapter '{}' does not support {unsupported}",
                client.getattr("adapter_identity")?.getattr("name")?.str()?
            ),))?,
    ))
}

/// Every persisted object state record, or none when the metadata table does not exist.
fn load_object_state_records<'py>(
    client: &Bound<'py, PyAny>,
    metadata_database: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = client.py();
    let statement: Bound<'py, PyAny> =
        native(py, "render_clickhouse_object_state_records_query")?.call1((metadata_database,))?;
    let rows: Bound<'py, PyAny> = match call(
        &client.getattr("query_many")?,
        &[
            ("statement", statement),
            ("decode", native(py, "decode_object_state_metadata_row")?),
        ],
    ) {
        Ok(rows) => rows,
        Err(failure)
            if failure.value(py).is_instance(
                &py.import(ADAPTER_EXCEPTIONS)?
                    .getattr("AdapterRelationNotFoundError")?,
            )? =>
        {
            return Ok(PyTuple::empty(py).into_any());
        }
        Err(failure) => return Err(failure),
    };
    let key_class: Bound<'py, PyAny> = py.import(COMPILE_MODELS)?.getattr("ObjectKey")?;
    let record_class: Bound<'py, PyAny> =
        py.import(PLANNER_MODELS)?.getattr("ObjectStateRecord")?;
    let records: Vec<Bound<'py, PyAny>> = items(&rows)?
        .iter()
        .map(|row| {
            let key: Bound<'py, PyAny> = call(
                &key_class,
                &[
                    ("database", row.getattr("database_name")?),
                    ("object_type", row.getattr("object_type")?),
                    ("name", row.getattr("object_name")?),
                ],
            )?;
            call(
                &record_class,
                &[
                    ("deployment_id", row.getattr("deployment_id")?),
                    ("key", key),
                    (
                        "normalized_fingerprint",
                        row.getattr("normalized_fingerprint")?,
                    ),
                    ("normalized_query", row.getattr("normalized_query")?),
                    ("recorded_at", row.getattr("recorded_at")?),
                    ("observation_id", row.getattr("observation_id")?),
                    ("state_kind", row.getattr("state_kind")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    tuple(py, records)
}

/// One immutable catalog and metadata snapshot after capability validation.
#[pyfunction]
#[pyo3(signature = (*, client, database))]
fn load_planning_warehouse_snapshot<'py>(
    py: Python<'py>,
    client: &Bound<'py, PyAny>,
    database: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !client
        .getattr("capabilities")?
        .getattr("virtual_environments")?
        .is_truthy()?
    {
        return Err(capability_error(client, "virtual environments")?);
    }
    let catalog: Bound<'py, PyAny> = client.call_method1("load_catalog", (database,))?;
    let records: Bound<'py, PyAny> = load_object_state_records(client, database)?;
    build(
        py,
        PLANNER_MODELS,
        "PlanningWarehouseSnapshot",
        &[("catalog", catalog), ("object_state_records", records)],
    )
}

/// The current active actual state from warehouse inspection.
#[pyfunction]
#[pyo3(signature = (*, client, desired_state, database))]
fn load_actual_state<'py>(
    py: Python<'py>,
    client: &Bound<'py, PyAny>,
    desired_state: &Bound<'py, PyAny>,
    database: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let snapshot: Bound<'py, PyAny> = load_planning_warehouse_snapshot(py, client, database)?;
    call(
        &native(py, "load_actual_state_from_snapshot")?,
        &[
            ("snapshot", snapshot),
            ("desired_state", desired_state.clone()),
            ("database", database.clone()),
        ],
    )
}

/// The target catalog and direct fingerprints direct planning reads.
#[pyfunction]
#[pyo3(signature = (*, client, database, metadata_database = None, logical_model_identities = None))]
fn load_direct_warehouse_snapshot<'py>(
    py: Python<'py>,
    client: &Bound<'py, PyAny>,
    database: &Bound<'py, PyAny>,
    metadata_database: Option<Bound<'py, PyAny>>,
    logical_model_identities: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    if !client
        .getattr("capabilities")?
        .getattr("direct_rebuild")?
        .is_truthy()?
    {
        return Err(capability_error(client, "direct rebuilds")?);
    }
    let catalog: Bound<'py, PyAny> = client.call_method1("load_catalog", (database,))?;
    let fingerprint_database: Bound<'py, PyAny> = match metadata_database {
        Some(metadata) if metadata.is_truthy()? => metadata,
        _ => database.clone(),
    };
    let fingerprints: Bound<'py, PyAny> = call(
        &client.getattr("load_direct_fingerprints")?,
        &[
            ("database", fingerprint_database),
            (
                "logical_model_identities",
                logical_model_identities.unwrap_or_else(|| PyTuple::empty(py).into_any()),
            ),
        ],
    )?;
    build(
        py,
        PLANNER_MODELS,
        "DirectWarehouseSnapshot",
        &[("catalog", catalog), ("fingerprints", fingerprints)],
    )
}

fn binding_triples<'py>(bindings: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = bindings.py();
    let triples: Vec<Bound<'py, PyAny>> = items(bindings)?
        .iter()
        .map(|binding| {
            tuple(
                py,
                vec![
                    binding.getattr("database")?,
                    binding.getattr("logical_name")?,
                    binding.getattr("physical_name")?,
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    Ok(PyList::new(py, triples)?.into_any())
}

/// One managed root classified as active, greenfield or damaged.
#[pyfunction]
#[pyo3(signature = (*, inspected_state, root_key))]
pub(crate) fn inspect_root_deployment_state<'py>(
    py: Python<'py>,
    inspected_state: &Bound<'py, PyAny>,
    root_key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let classified: Bound<'py, PyAny> = native(py, "root_deployment_state")?.call1((
        binding_triples(&inspected_state.getattr("active_bindings")?)?,
        binding_triples(&inspected_state.getattr("physical_candidates")?)?,
        root_key.getattr("database")?,
        root_key.getattr("name")?,
    ))?;
    let (state_kind, active_deployment_id): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
        classified.extract()?;
    build(
        py,
        PLANNER_MODELS,
        "RootDeploymentInspection",
        &[
            ("root_key", root_key.clone()),
            (
                "state_kind",
                py.import(PLANNER_TYPES)?
                    .getattr("RootDeploymentStateKind")?
                    .call1((state_kind,))?,
            ),
            ("active_deployment_id", active_deployment_id),
        ],
    )
}

/// Managed-table inspection from one catalog snapshot, without further warehouse reads.
#[pyfunction]
#[pyo3(signature = (*, catalog))]
pub(crate) fn build_inspected_managed_table_state_from_catalog<'py>(
    py: Python<'py>,
    catalog: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let database: Bound<'py, PyAny> = catalog.getattr("identity")?.getattr("database")?;
    let relations: Vec<Bound<'py, PyAny>> = items(&catalog.getattr("relations")?)?
        .iter()
        .map(|relation| {
            tuple(
                py,
                vec![
                    relation.getattr("name")?,
                    object(py, !relation.getattr("query_sql")?.is_none())?,
                    relation.getattr("stable_binding_name")?,
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let (bindings, candidates): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
        native(py, "managed_catalog_relations")?
            .call1((PyList::new(py, relations)?,))?
            .extract()?;
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    let binding_class: Bound<'py, PyAny> = models.getattr("InspectedActiveTableBinding")?;
    let active: Vec<Bound<'py, PyAny>> = items(&bindings)?
        .iter()
        .map(|binding| {
            let (logical_name, physical_name): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
                binding.extract()?;
            call(
                &binding_class,
                &[
                    ("database", database.clone()),
                    ("logical_name", logical_name),
                    ("physical_name", physical_name),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let candidate_class: Bound<'py, PyAny> = models.getattr("InspectedPhysicalTableCandidate")?;
    let physical: Vec<Bound<'py, PyAny>> = items(&candidates)?
        .iter()
        .map(|candidate| {
            let (logical_name, physical_name, is_view): (
                Bound<'py, PyAny>,
                Bound<'py, PyAny>,
                Bound<'py, PyAny>,
            ) = candidate.extract()?;
            call(
                &candidate_class,
                &[
                    ("database", database.clone()),
                    ("logical_name", logical_name),
                    ("physical_name", physical_name),
                    (
                        "object_type",
                        object(
                            py,
                            if is_view.is_truthy()? {
                                "view"
                            } else {
                                "table"
                            },
                        )?,
                    ),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    build(
        py,
        ADAPTER_MODELS,
        "InspectedManagedTableState",
        &[
            ("active_bindings", tuple(py, active)?),
            ("physical_candidates", tuple(py, physical)?),
        ],
    )
}

/// The deployment-suffixed physical name of one logical object.
#[pyfunction]
#[pyo3(signature = (*, logical_name, deployment_id))]
fn build_deployment_physical_name(logical_name: &str, deployment_id: &str) -> String {
    format!("{logical_name}{DEPLOYMENT_SEPARATOR}{deployment_id}")
}

/// The deployment identifier after the last separator of a physical object name.
#[pyfunction]
pub(crate) fn deployment_id_from_physical_name(physical_name: &str) -> PyResult<String> {
    physical_name
        .rsplit_once(DEPLOYMENT_SEPARATOR)
        .map(|(_, deployment_id)| deployment_id.to_owned())
        .ok_or_else(|| PyIndexError::new_err("list index out of range"))
}

/// The logical object name before the last separator of a physical object name.
#[pyfunction]
pub(crate) fn logical_name_from_physical_name(physical_name: &str) -> String {
    physical_name
        .rsplit_once(DEPLOYMENT_SEPARATOR)
        .map_or(physical_name, |(logical_name, _)| logical_name)
        .to_owned()
}

/// `value` with each dataclass and non-dict mapping `json.dumps` cannot encode replaced the way
/// a normalized fingerprint's `default` hook replaces it.
fn fingerprint_payload<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = value.py();
    if value.is_none()
        || value.is_instance_of::<pyo3::types::PyString>()
        || value.is_instance_of::<pyo3::types::PyInt>()
        || value.is_instance_of::<pyo3::types::PyFloat>()
    {
        return Ok(value.clone());
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        let converted: Vec<Bound<'py, PyAny>> = items(value)?
            .iter()
            .map(fingerprint_payload)
            .collect::<PyResult<_>>()?;
        return Ok(PyList::new(py, converted)?.into_any());
    }
    if let Ok(entries) = value.cast::<PyDict>() {
        let converted: Bound<'py, PyDict> = PyDict::new(py);
        for (key, item) in entries.iter() {
            converted.set_item(key, fingerprint_payload(&item)?)?;
        }
        return Ok(converted.into_any());
    }
    let dataclasses: Bound<'py, PyModule> = py.import("dataclasses")?;
    if dataclasses
        .call_method1("is_dataclass", (value,))?
        .is_truthy()?
        && !value.is_instance_of::<pyo3::types::PyType>()
    {
        let converted: Bound<'py, PyDict> = PyDict::new(py);
        for field in items(&dataclasses.call_method1("fields", (value,))?)? {
            let name: Bound<'py, PyAny> = field.getattr("name")?;
            converted.set_item(
                &name,
                fingerprint_payload(&value.getattr(name.cast::<pyo3::types::PyString>()?)?)?,
            )?;
        }
        return Ok(converted.into_any());
    }
    if value.is_instance(&py.import("collections.abc")?.getattr("Mapping")?)? {
        return fingerprint_payload(&py.get_type::<PyDict>().call1((value,))?);
    }
    Err(plan_error(
        py,
        format!(
            "Cannot serialize {} in a normalized fingerprint",
            value.get_type().name()?
        ),
    )?)
}

/// A deterministic comparable fingerprint payload string: sorted-key JSON of `value`, with
/// dataclasses as their fields and mappings as dictionaries.
#[pyfunction]
pub(crate) fn build_normalized_fingerprint(value: &Bound<'_, PyAny>) -> PyResult<String> {
    json_dumps(&fingerprint_payload(value)?, None, true)
}

/// `(name, kind, relation, columns, storage)` of one compiled model, as the direct fingerprint
/// bindings take it.
fn native_identity<'py>(model: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = model.py();
    let storage: Bound<'py, PyAny> = if is_a(model, COMPILE_MODELS, "CompiledTableModel")? {
        let transform: Bound<'py, PyAny> = model.getattr("transform")?;
        let settings: Bound<'py, PyAny> = transform.getattr("settings")?;
        let settings: Bound<'py, PyAny> = if settings.is_none() {
            settings
        } else {
            py.get_type::<PyList>()
                .call1((settings.call_method0("items")?,))?
        };
        tuple(
            py,
            vec![
                transform.getattr("engine")?,
                py.get_type::<PyList>()
                    .call1((transform.getattr("order_by")?,))?,
                transform.getattr("partition_by")?,
                transform.getattr("ttl")?,
                settings,
            ],
        )?
    } else {
        none(py)
    };
    let columns: Vec<Bound<'py, PyAny>> = items(&model.getattr("output_columns")?)?
        .iter()
        .map(|column| tuple(py, vec![column.getattr("name")?, column.getattr("type")?]))
        .collect::<PyResult<_>>()?;
    tuple(
        py,
        vec![
            model.getattr("key")?.getattr("name")?,
            model.getattr("kind")?.str()?.into_any(),
            model.getattr("relation_name")?,
            PyList::new(py, columns)?.into_any(),
            storage,
        ],
    )
}

/// Canonical semantic direct model fingerprints and their comparison.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct DirectModelFingerprint;

#[pymethods]
impl DirectModelFingerprint {
    /// The allowlisted semantic identity persisted for changed selection.
    #[staticmethod]
    #[pyo3(signature = (*, model))]
    fn identity<'py>(py: Python<'py>, model: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let metadata: Bound<'py, PyAny> =
            native(py, "direct_model_identity_metadata")?.call1((native_identity(model)?,))?;
        py.import("json")?.call_method1("loads", (metadata,))
    }

    /// The SHA-256 hex digest of one query's UTF-8 text.
    #[staticmethod]
    fn query_hash(query_sql: &str) -> String {
        sha256_hex(query_sql)
    }

    /// The fingerprint record a direct build writes for one applied model.
    #[staticmethod]
    #[pyo3(signature = (*, model, database, workflow_id, tool_version))]
    pub(crate) fn record<'py>(
        py: Python<'py>,
        model: &Bound<'py, PyAny>,
        database: &Bound<'py, PyAny>,
        workflow_id: &Bound<'py, PyAny>,
        tool_version: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let query: Bound<'py, PyAny> = model.getattr("query")?;
        let (fingerprint_id, logical_identity, definition_hash, identity_metadata): (
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
        ) = native(py, "direct_fingerprint_record")?
            .call1((native_identity(model)?, &query, database, workflow_id))?
            .extract()?;
        build(
            py,
            ADAPTER_MODELS,
            "AdapterDirectFingerprintRecord",
            &[
                ("fingerprint_id", fingerprint_id),
                ("logical_model_identity", logical_identity),
                ("definition_sql", query),
                ("definition_hash", definition_hash),
                ("identity_metadata", identity_metadata),
                ("workflow_id", workflow_id.clone()),
                ("tool_version", tool_version.clone()),
            ],
        )
    }

    /// Direct fingerprint differences shared by native planning and the UI.
    #[staticmethod]
    #[pyo3(signature = (*, model, baseline))]
    fn drift_reasons<'py>(
        py: Python<'py>,
        model: &Bound<'py, PyAny>,
        baseline: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let baseline: Bound<'py, PyAny> = if baseline.is_none() {
            none(py)
        } else {
            tuple(
                py,
                vec![
                    baseline.getattr("definition_hash")?,
                    baseline.getattr("identity_metadata")?,
                ],
            )?
        };
        let reasons: Bound<'py, PyAny> = native(py, "direct_model_drift_reasons")?.call1((
            native_identity(model)?,
            model.getattr("query")?,
            baseline,
        ))?;
        py.get_type::<PyTuple>().call1((reasons,))
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_adapter_resource, module)?)?;
    module.add_function(wrap_pyfunction!(build_shadow_adapter_resource, module)?)?;
    module.add_function(wrap_pyfunction!(build_adapter_replay_query, module)?)?;
    module.add_function(wrap_pyfunction!(build_metadata_state, module)?)?;
    module.add_function(wrap_pyfunction!(build_adapter_metadata_state, module)?)?;
    module.add_function(wrap_pyfunction!(decode_object_state_metadata_row, module)?)?;
    module.add_function(wrap_pyfunction!(load_planning_warehouse_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(load_actual_state, module)?)?;
    module.add_function(wrap_pyfunction!(load_direct_warehouse_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(inspect_root_deployment_state, module)?)?;
    module.add_function(wrap_pyfunction!(
        build_inspected_managed_table_state_from_catalog,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(build_deployment_physical_name, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_id_from_physical_name, module)?)?;
    module.add_function(wrap_pyfunction!(logical_name_from_physical_name, module)?)?;
    module.add_function(wrap_pyfunction!(build_normalized_fingerprint, module)?)?;
    module.add_class::<DirectModelFingerprint>()?;
    Ok(())
}
