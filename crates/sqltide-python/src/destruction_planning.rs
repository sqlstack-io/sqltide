//! Read-only assembly of frozen destruction plans: the selection, scope, warehouse evidence,
//! challenges and fingerprints of one plan. Warehouse reads go through the Python planning
//! connection; the decisions are the native destruction planner's.

use std::collections::BTreeSet;

use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple, PyType};
use sqltide_planner::direct::sha256_hex;

use crate::artifacts::{json_dumps, json_dumps_compact};
use crate::clickhouse_catalog::{build, object, quote};
use crate::destruction::{
    EvidenceTuple, OwnedTuple, ProjectArgument, RequestArgument, build_destruction_evidence,
    destruction_available_pipelines, destruction_plan_fingerprint,
    list_destruction_inactive_pipelines, plan_destruction_scope, py_build_destruction_challenges,
    resolve_destruction_selection,
};
use crate::executor::order_relations_for_drop;
use crate::model_sql::chained;

const MODELS: &str = "sqltide.executor.destruction.models";
const TYPES: &str = "sqltide.executor.destruction.types";
const EXCEPTIONS: &str = "sqltide.executor.destruction.exceptions";
const CONSTANTS: &str = "sqltide.executor.destruction.constants";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const PRODUCTION_TARGET_NAMES: [&str; 2] = ["prod", "production"];
const DUPLICATE_RELATION_ORDERING_ERROR: &str = "duplicate";

type Value<'py> = Bound<'py, PyAny>;
/// `(name, is a source, owning pipeline name, (resource name, resource kind))` of one graph key.
type GraphKey = (String, bool, Option<String>, Vec<(String, String)>);
/// `(model names, source names, owned relations, relation evidence)` of one planned scope.
type ScopedEvidence = (
    Vec<String>,
    Vec<String>,
    Vec<OwnedTuple>,
    Vec<EvidenceTuple>,
);

fn exception<'py>(
    py: Python<'py>,
    class: &str,
    argument: impl IntoPyObject<'py>,
) -> PyResult<Value<'py>> {
    py.import(EXCEPTIONS)?.getattr(class)?.call1((argument,))
}

fn raise<'py>(py: Python<'py>, class: &str, argument: impl IntoPyObject<'py>) -> PyErr {
    match exception(py, class, argument) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// `raise error from None` while handling `handled`.
fn from_none(error: Value<'_>, handled: &PyErr) -> PyResult<PyErr> {
    chained(&error, handled.value(error.py()), None)?;
    Ok(PyErr::from_value(error))
}

/// `getattr(value, name, default)`.
fn getattr_or<'py>(value: &Value<'py>, name: &str, default: Value<'py>) -> PyResult<Value<'py>> {
    match value.getattr(name) {
        Ok(found) => Ok(found),
        Err(error) if error.is_instance_of::<PyAttributeError>(value.py()) => Ok(default),
        Err(error) => Err(error),
    }
}

fn string_tuple<'py>(py: Python<'py>, values: &[String]) -> PyResult<Value<'py>> {
    object(py, PyTuple::new(py, values)?)
}

/// `sorted(values)` with Python ordering.
fn sorted<'py>(py: Python<'py>, values: &Value<'py>) -> PyResult<Value<'py>> {
    py.import("builtins")?.call_method1("sorted", (values,))
}

/// The typed destruction failure a native planner `ValueError((code, detail))` stands for; any
/// other `ValueError` propagates unchanged.
fn planning_error(py: Python<'_>, error: PyErr) -> PyResult<PyErr> {
    if !error.is_instance_of::<PyValueError>(py) {
        return Ok(error);
    }
    let arguments: Value<'_> = error.value(py).getattr("args")?;
    if arguments.len()? == 0 || !arguments.get_item(0)?.is_instance_of::<PyTuple>() {
        return Ok(error);
    }
    let reason: Value<'_> = arguments.get_item(0)?;
    let code: String = match py
        .import(TYPES)?
        .getattr("DestructionPlanningErrorCode")?
        .call1((reason.get_item(0)?,))
    {
        Ok(code) => code.getattr("value")?.extract()?,
        Err(failure) => return raised_while_handling(py, failure, &error),
    };
    let detail: Value<'_> = reason.get_item(1)?;
    let is_list: bool = detail.is_instance_of::<PyList>();
    let mapped: Value<'_> = match code.as_str() {
        "selection" => exception(py, "DestructionSelectionError", detail.str()?)?,
        "unknown_pipelines" if is_list => exception(
            py,
            "DestructionSelectionError",
            format!(
                "Unknown pipelines: {}",
                PyTuple::new(py, detail.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.repr()?
            ),
        )?,
        "dependency" if is_list => exception(
            py,
            "DestructionDependencyError",
            PyTuple::new(py, detail.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
        )?,
        "unsupported_historical_kind" if detail.is_instance_of::<PyTuple>() => {
            let fields: Vec<Value<'_>> = detail.try_iter()?.take(3).collect::<PyResult<_>>()?;
            let [kind, name] = <[Value<'_>; 2]>::try_from(fields).map_err(|fields| {
                PyValueError::new_err(if fields.len() < 2 {
                    format!(
                        "not enough values to unpack (expected 2, got {})",
                        fields.len()
                    )
                } else {
                    "too many values to unpack (expected 2)".to_owned()
                })
            })?;
            exception(
                py,
                "DestructionResourceError",
                format!(
                    "Unsupported historical manifest resource kind {} for {}",
                    kind.repr()?,
                    name.repr()?
                ),
            )?
        }
        _ => {
            if code == "invalid_object_type"
                && let Err(failure) = py
                    .import("sqltide.compiler.compile.types")?
                    .getattr("DesiredObjectType")?
                    .call1((detail.str()?,))
            {
                return raised_while_handling(py, failure, &error);
            }
            exception(py, "DestructionResourceError", detail.str()?)?
        }
    };
    from_none(mapped, &error)
}

/// A failure raised while `handled` is being handled.
fn raised_while_handling(py: Python<'_>, failure: PyErr, handled: &PyErr) -> PyResult<PyErr> {
    if failure.value(py).getattr("__context__")?.is_none() {
        failure
            .value(py)
            .setattr("__context__", handled.value(py))?;
    }
    Ok(failure)
}

fn current_pipeline_names(request: &Value<'_>, analysis: &Value<'_>) -> PyResult<Vec<String>> {
    let py: Python<'_> = request.py();
    let project: Value<'_> = analysis.getattr("realized_project")?.getattr("project")?;
    let names: Vec<Value<'_>> = project
        .getattr("pipelines")?
        .try_iter()?
        .map(|pipeline| pipeline?.getattr("pipeline")?.getattr("name"))
        .collect::<PyResult<_>>()?;
    let names: Vec<String> = sorted(py, &PyList::new(py, names)?.into_any())?.extract()?;
    let compiled_target: Value<'_> = project.getattr("target_name")?;
    let target: Value<'_> = request.getattr("target")?;
    if !compiled_target.is_none() && target.ne(&compiled_target)? {
        return Err(raise(
            py,
            "DestructionSelectionError",
            format!(
                "Requested target {} does not match compiled target {}",
                target.repr()?,
                compiled_target.repr()?
            ),
        ));
    }
    Ok(names)
}

fn load_manifest_snapshot<'py>(
    request: &Value<'py>,
    analysis: &Value<'py>,
    connection: &Value<'py>,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = request.py();
    let native: Bound<'py, PyModule> = py.import("sqltide._native")?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("analysis", analysis)?;
    let project_identity: Value<'py> = native
        .getattr("resolve_manifest_project_identity")?
        .call((), Some(&keywords))?;
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("database", request.getattr("metadata_database")?)?;
    keywords.set_item("project_identity", project_identity)?;
    keywords.set_item("target_name", request.getattr("target")?)?;
    keywords.set_item("target_database", request.getattr("database")?)?;
    let snapshot: Value<'py> = connection.call_method("load_manifests", (), Some(&keywords))?;
    let unavailable: Value<'py> = py
        .import("sqltide.adapter.types")?
        .getattr("AdapterOptionalStateStatus")?
        .getattr("UNAVAILABLE")?;
    if snapshot.getattr("status")?.eq(unavailable)? {
        let warning: Value<'py> = snapshot.getattr("warning")?;
        let message: Value<'py> = if warning.is_truthy()? {
            warning
        } else {
            object(py, "Manifest history is unavailable")?
        };
        return Err(raise(py, "DestructionResourceError", message));
    }
    let manifest_version: Value<'py> = native.getattr("MANIFEST_VERSION")?;
    for manifest in snapshot.getattr("manifests")?.try_iter()? {
        let version: Value<'py> = manifest?.getattr("manifest_version")?;
        if version.ne(&manifest_version)? {
            return Err(raise(
                py,
                "DestructionResourceError",
                format!(
                    "Unsupported manifest version {}; expected {}",
                    version.str()?,
                    manifest_version.str()?
                ),
            ));
        }
    }
    Ok(snapshot)
}

fn resource_kind(resource: &Value<'_>) -> PyResult<&'static str> {
    let py: Python<'_> = resource.py();
    let models: Bound<'_, PyModule> = py.import(ADAPTER_MODELS)?;
    for (class, kind) in [
        ("AdapterView", "view"),
        ("AdapterMaterializedView", "materialized_view"),
        ("AdapterManagedSource", "managed_source"),
        ("AdapterTable", "table"),
    ] {
        if resource.is_instance(&models.getattr(class)?)? {
            return Ok(kind);
        }
    }
    Err(raise(
        py,
        "DestructionResourceError",
        format!(
            "Unsupported manifest resource: {}",
            resource.get_type().getattr("__name__")?.str()?
        ),
    ))
}

/// The compiled project as the native planner's indexed logical-key graph.
fn native_project(analysis: &Value<'_>) -> PyResult<ProjectArgument> {
    let py: Python<'_> = analysis.py();
    let realized: Value<'_> = analysis.getattr("realized_project")?;
    let project: Value<'_> = realized.getattr("project")?;
    let resources_by_key: Value<'_> = realized.getattr("resources_by_logical_key")?;
    let graph: Value<'_> = analysis.getattr("graph")?;
    let mut candidates: Vec<Value<'_>> = graph
        .getattr("ordered_keys")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    for model in project.getattr("models")?.try_iter()? {
        candidates.push(model?.getattr("key")?);
    }
    for source in project.getattr("sources")?.try_iter()? {
        candidates.push(source?.getattr("key")?);
    }
    for pipeline in project.getattr("pipelines")?.try_iter()? {
        let source: Value<'_> = pipeline?.getattr("source")?;
        if !source.is_none() {
            candidates.push(source.getattr("key")?);
        }
    }
    for key in resources_by_key.try_iter()? {
        candidates.push(key?);
    }
    let index_by_key: Bound<'_, PyDict> = PyDict::new(py);
    for key in candidates {
        index_by_key.call_method1("setdefault", (key, index_by_key.len()))?;
    }
    let pipeline_by_model_key: Bound<'_, PyDict> = PyDict::new(py);
    for model in project.getattr("models")?.try_iter()? {
        let model: Value<'_> = model?;
        pipeline_by_model_key.set_item(model.getattr("key")?, model.getattr("pipeline_name")?)?;
    }
    let source_type: Value<'_> = py
        .import("sqltide.compiler.compile.types")?
        .getattr("LogicalResourceType")?
        .getattr("SOURCE")?;
    let upstream_edges: Value<'_> = graph.getattr("upstream_edges_by_key")?;
    let downstream_edges: Value<'_> = graph.getattr("downstream_edges_by_key")?;
    let empty: Bound<'_, PyTuple> = PyTuple::empty(py);
    let position =
        |key: Value<'_>| -> PyResult<usize> { index_by_key.as_any().get_item(key)?.extract() };
    let positions = |edges: &Value<'_>, key: &Value<'_>, side: &str| -> PyResult<Vec<usize>> {
        edges
            .call_method1("get", (key, &empty))?
            .try_iter()?
            .map(|edge| position(edge?.getattr(side)?))
            .collect()
    };
    let mut keys: Vec<GraphKey> = Vec::new();
    let mut upstream: Vec<Vec<usize>> = Vec::new();
    let mut downstream: Vec<Vec<usize>> = Vec::new();
    for key in index_by_key.keys() {
        let resources: Vec<(String, String)> = resources_by_key
            .call_method1("get", (&key, &empty))?
            .try_iter()?
            .map(|resource| {
                let resource: Value<'_> = resource?;
                Ok((
                    resource.getattr("name")?.extract()?,
                    resource_kind(&resource)?.to_owned(),
                ))
            })
            .collect::<PyResult<_>>()?;
        keys.push((
            key.getattr("name")?.extract()?,
            key.getattr("resource_type")?.eq(&source_type)?,
            pipeline_by_model_key
                .get_item(&key)?
                .map(|name| name.extract())
                .transpose()?,
            resources,
        ));
        upstream.push(positions(&upstream_edges, &key, "upstream_key")?);
        downstream.push(positions(&downstream_edges, &key, "downstream_key")?);
    }
    let pipelines: Vec<(String, Option<usize>)> = project
        .getattr("pipelines")?
        .try_iter()?
        .map(|pipeline| {
            let pipeline: Value<'_> = pipeline?;
            let source: Value<'_> = pipeline.getattr("source")?;
            Ok((
                pipeline.getattr("pipeline")?.getattr("name")?.extract()?,
                if source.is_none() {
                    None
                } else {
                    Some(position(source.getattr("key")?)?)
                },
            ))
        })
        .collect::<PyResult<_>>()?;
    let indices = |name: &str| -> PyResult<Vec<usize>> {
        project
            .getattr(name)?
            .try_iter()?
            .map(|item| position(item?.getattr("key")?))
            .collect()
    };
    Ok((
        keys,
        upstream,
        downstream,
        pipelines,
        indices("sources")?,
        indices("models")?,
    ))
}

fn relation_stats<'py>(
    connection: &Value<'py>,
    database: &str,
    relation_names: &[String],
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = connection.py();
    let stats: Bound<'py, PyDict> = PyDict::new(py);
    if relation_names.is_empty() {
        return Ok(stats);
    }
    let names: Vec<String> = relation_names.iter().map(|name| quote(name)).collect();
    let rows: Value<'py> = connection
        .call_method1(
            "query",
            (format!(
                "SELECT table AS relation_name, sum(bytes_on_disk) AS total_bytes, \
                 count() AS active_parts FROM system.parts WHERE active = 1 \
                 AND database = {} AND table IN ({}) GROUP BY table ORDER BY table",
                quote(database),
                names.join(", ")
            ),),
        )?
        .call_method0("named_rows")?;
    let integer = |value: Value<'py>| -> PyResult<Value<'py>> {
        py.get_type::<PyInt>().call1((value.str()?,))
    };
    for row in rows.try_iter()? {
        let row: Value<'py> = row?;
        let name: Value<'py> = row.get_item("relation_name")?.str()?.into_any();
        let total_bytes: Value<'py> = integer(row.get_item("total_bytes")?)?;
        let active_parts: Value<'py> = integer(row.get_item("active_parts")?)?;
        stats.set_item(name, (total_bytes, active_parts))?;
    }
    Ok(stats)
}

fn relation_evidence<'py>(py: Python<'py>, native: &EvidenceTuple) -> PyResult<Value<'py>> {
    let (
        database,
        name,
        kind,
        exists,
        total_bytes,
        active_parts,
        catalog_fingerprint,
        logical_names,
        pipeline_names,
        ownership,
        dependency_relation_names,
    ) = native;
    let ownership_type: Value<'py> = py.import(TYPES)?.getattr("DestructionOwnership")?;
    let ownership: Vec<Value<'py>> = ownership
        .iter()
        .map(|value| ownership_type.call1((value,)))
        .collect::<PyResult<_>>()?;
    build(
        py,
        MODELS,
        "DestructionRelationEvidence",
        &[
            ("database", object(py, database)?),
            ("name", object(py, name)?),
            ("kind", object(py, kind)?),
            ("exists", object(py, *exists)?),
            ("total_bytes", object(py, *total_bytes)?),
            ("active_parts", object(py, *active_parts)?),
            (
                "catalog_fingerprint",
                object(py, catalog_fingerprint.clone())?,
            ),
            ("logical_names", string_tuple(py, logical_names)?),
            ("pipeline_names", string_tuple(py, pipeline_names)?),
            ("ownership", object(py, PyTuple::new(py, ownership)?)?),
            (
                "dependency_relation_names",
                string_tuple(py, dependency_relation_names)?,
            ),
        ],
    )
}

/// Reject duplicate relations and dependency cycles the drop order could not resolve.
fn check_drop_order(py: Python<'_>, relations: &[EvidenceTuple]) -> PyResult<()> {
    let ordering: Vec<(String, Vec<String>)> = relations
        .iter()
        .map(|relation| (relation.1.clone(), relation.10.clone()))
        .collect();
    let Err(error) = order_relations_for_drop(ordering) else {
        return Ok(());
    };
    let arguments: Value<'_> = error.value(py).getattr("args")?;
    let message: String = if arguments
        .get_item(0)?
        .eq(DUPLICATE_RELATION_ORDERING_ERROR)?
    {
        let position: usize = arguments.get_item(1)?.extract()?;
        format!(
            "Destruction evidence contains duplicate relation {}",
            PyString::new(py, &relations[position].1).repr()?
        )
    } else {
        let unresolved: Vec<Value<'_>> = arguments
            .get_item(1)?
            .try_iter()?
            .collect::<PyResult<_>>()?;
        format!(
            "Destruction relation dependency cycle blocks ordering: {}",
            PyTuple::new(py, unresolved)?.repr()?
        )
    };
    Err(from_none(
        exception(py, "DestructionConsistencyError", message)?,
        &error,
    )?)
}

/// `_canonical`: enums by value, datetimes as ISO text, paths as text, dataclasses and mappings
/// as objects, sequences as arrays with sets ordered by their JSON text, anything else by repr.
fn canonical<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = value.py();
    if value.is_instance(&py.import("enum")?.getattr("Enum")?)? {
        return value.getattr("value");
    }
    if value.is_instance(&py.import("datetime")?.getattr("datetime")?)? {
        return value.call_method0("isoformat");
    }
    if value.is_instance(&py.import("pathlib")?.getattr("Path")?)? {
        return Ok(value.str()?.into_any());
    }
    let dataclasses: Bound<'py, PyModule> = py.import("dataclasses")?;
    if dataclasses
        .call_method1("is_dataclass", (value,))?
        .is_truthy()?
        && !value.is_instance_of::<PyType>()
    {
        let converted: Bound<'py, PyDict> = PyDict::new(py);
        for field in dataclasses.call_method1("fields", (value,))?.try_iter()? {
            let name: Value<'py> = field?.getattr("name")?;
            converted.set_item(&name, canonical(&value.getattr(name.cast::<PyString>()?)?)?)?;
        }
        return Ok(converted.into_any());
    }
    if value.is_instance(&py.import("collections.abc")?.getattr("Mapping")?)? {
        let mut entries: Vec<(String, Value<'py>, Value<'py>)> = Vec::new();
        for item in value.call_method0("items")?.try_iter()? {
            let (key, item): (Value<'py>, Value<'py>) = item?.extract()?;
            let text: Bound<'py, PyString> = key.str()?;
            entries.push((text.to_string(), text.into_any(), item));
        }
        entries.sort_by(|left, right| left.0.cmp(&right.0));
        let converted: Bound<'py, PyDict> = PyDict::new(py);
        for (_, key, item) in entries {
            converted.set_item(key, canonical(&item)?)?;
        }
        return Ok(converted.into_any());
    }
    let is_set: bool = value.is_instance_of::<PySet>() || value.is_instance_of::<PyFrozenSet>();
    if is_set || value.is_instance_of::<PyTuple>() || value.is_instance_of::<PyList>() {
        let mut items: Vec<Value<'py>> = value
            .try_iter()?
            .map(|item| canonical(&item?))
            .collect::<PyResult<_>>()?;
        if is_set {
            let mut keyed: Vec<(String, Value<'py>)> = items
                .into_iter()
                .map(|item| Ok((json_dumps(&item, None, true)?, item)))
                .collect::<PyResult<_>>()?;
            keyed.sort_by(|left, right| left.0.cmp(&right.0));
            items = keyed.into_iter().map(|(_, item)| item).collect();
        }
        return Ok(PyList::new(py, items)?.into_any());
    }
    if value.is_none()
        || value.is_instance_of::<PyString>()
        || value.is_instance_of::<PyInt>()
        || value.is_instance_of::<pyo3::types::PyFloat>()
    {
        return Ok(value.clone());
    }
    Ok(value.repr()?.into_any())
}

/// Sort `(name, item)` pairs by name, keeping the original order of equal names.
fn by_name<'py>(
    items: Value<'py>,
    name: impl Fn(&Value<'py>) -> PyResult<String>,
) -> PyResult<Vec<Value<'py>>> {
    let mut keyed: Vec<(String, Value<'py>)> = items
        .try_iter()?
        .map(|item| {
            let item: Value<'py> = item?;
            Ok((name(&item)?, item))
        })
        .collect::<PyResult<_>>()?;
    keyed.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(keyed.into_iter().map(|(_, item)| item).collect())
}

/// The fingerprint of everything in the compiled project a destruction plan depends on.
fn manifest_fingerprint(analysis: &Value<'_>) -> PyResult<String> {
    let py: Python<'_> = analysis.py();
    let none: Value<'_> = py.None().into_bound(py);
    let realized: Value<'_> = analysis.getattr("realized_project")?;
    let project: Value<'_> = realized.getattr("project")?;
    let relation_names: Value<'_> = realized.getattr("relation_name_by_logical_key")?;
    let queries: Value<'_> = realized.getattr("resolved_query_by_model_key")?;
    let resources: Value<'_> = realized.getattr("resources_by_logical_key")?;
    let empty: Bound<'_, PyTuple> = PyTuple::empty(py);
    let compile_inputs: Value<'_> = getattr_or(analysis, "compile_inputs", none.clone())?;
    let access_policy: Value<'_> = getattr_or(analysis, "access_policy", none.clone())?;
    let pipeline_names: Vec<Value<'_>> = project
        .getattr("pipelines")?
        .try_iter()?
        .map(|pipeline| pipeline?.getattr("pipeline")?.getattr("name"))
        .collect::<PyResult<_>>()?;
    let key_name =
        |item: &Value<'_>| -> PyResult<String> { item.getattr("key")?.getattr("name")?.extract() };
    let mut models: Vec<Value<'_>> = Vec::new();
    for model in by_name(project.getattr("models")?, key_name)? {
        let key: Value<'_> = model.getattr("key")?;
        let payload: Bound<'_, PyDict> = PyDict::new(py);
        payload.set_item("name", key.getattr("name")?)?;
        payload.set_item("pipeline", model.getattr("pipeline_name")?)?;
        payload.set_item("kind", model.getattr("kind")?)?;
        payload.set_item(
            "relation_name",
            relation_names.call_method1("get", (&key,))?,
        )?;
        payload.set_item("query", queries.call_method1("get", (&key,))?)?;
        payload.set_item("resources", resources.call_method1("get", (&key, &empty))?)?;
        models.push(payload.into_any());
    }
    let mut sources: Vec<Value<'_>> = Vec::new();
    for source in by_name(project.getattr("sources")?, key_name)? {
        let key: Value<'_> = source.getattr("key")?;
        let payload: Bound<'_, PyDict> = PyDict::new(py);
        payload.set_item("name", key.getattr("name")?)?;
        payload.set_item("definition", getattr_or(&source, "source", none.clone())?)?;
        payload.set_item(
            "relation_name",
            relation_names.call_method1("get", (&key,))?,
        )?;
        payload.set_item("resources", resources.call_method1("get", (&key, &empty))?)?;
        sources.push(payload.into_any());
    }
    let mut edges: Vec<(String, String, String, Value<'_>)> = Vec::new();
    for group in analysis
        .getattr("graph")?
        .getattr("downstream_edges_by_key")?
        .call_method0("values")?
        .try_iter()?
    {
        for edge in group?.try_iter()? {
            let edge: Value<'_> = edge?;
            let edge_type: Value<'_> = edge.getattr("edge_type")?;
            edges.push((
                edge.getattr("upstream_key")?.getattr("name")?.extract()?,
                edge.getattr("downstream_key")?.getattr("name")?.extract()?,
                edge_type.str()?.extract()?,
                edge_type,
            ));
        }
    }
    edges.sort_by(|left, right| (&left.0, &left.1, &left.2).cmp(&(&right.0, &right.1, &right.2)));
    let edges: Vec<Value<'_>> = edges
        .into_iter()
        .map(|(upstream, downstream, _, edge_type)| object(py, (upstream, downstream, edge_type)))
        .collect::<PyResult<_>>()?;
    let payload: Bound<'_, PyDict> = PyDict::new(py);
    payload.set_item("project_name", project.getattr("project_name")?)?;
    payload.set_item("target_name", project.getattr("target_name")?)?;
    payload.set_item(
        "effective_target",
        getattr_or(&compile_inputs, "effective_target", none.clone())?,
    )?;
    payload.set_item(
        "variables",
        getattr_or(&compile_inputs, "variables", empty.clone().into_any())?,
    )?;
    payload.set_item(
        "access_policy_fingerprint",
        getattr_or(&access_policy, "fingerprint", none)?,
    )?;
    payload.set_item(
        "pipelines",
        sorted(py, &PyList::new(py, pipeline_names)?.into_any())?,
    )?;
    payload.set_item("models", PyTuple::new(py, models)?)?;
    payload.set_item("sources", PyTuple::new(py, sources)?)?;
    payload.set_item("edges", PyTuple::new(py, edges)?)?;
    Ok(sha256_hex(&json_dumps_compact(&canonical(
        &payload.into_any(),
    )?)?))
}

fn validated_created_at<'py>(
    py: Python<'py>,
    now: Option<Value<'py>>,
    ttl: &Value<'py>,
) -> PyResult<Value<'py>> {
    let datetime: Bound<'py, PyModule> = py.import("datetime")?;
    let created_at: Value<'py> = match now.filter(|now| now.is_truthy().unwrap_or(true)) {
        Some(now) => now,
        None => {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("tz", datetime.getattr("UTC")?)?;
            datetime
                .getattr("datetime")?
                .call_method("now", (), Some(&keywords))?
        }
    };
    if created_at.getattr("tzinfo")?.is_none() {
        return Err(raise(
            py,
            "DestructionValidationError",
            "Destruction plan timestamps must be timezone-aware",
        ));
    }
    if ttl.le(datetime.getattr("timedelta")?.call1((0,))?)? {
        return Err(raise(
            py,
            "DestructionValidationError",
            "Destruction plan TTL must be positive",
        ));
    }
    Ok(created_at)
}

fn production_reset(request: &Value<'_>, analysis: &Value<'_>) -> PyResult<bool> {
    let py: Python<'_> = request.py();
    let reset_target: Value<'_> = py
        .import(TYPES)?
        .getattr("DestructionOperation")?
        .getattr("RESET_TARGET")?;
    if !request.getattr("operation")?.eq(reset_target)? {
        return Ok(false);
    }
    let target: String = request
        .getattr("target")?
        .call_method0("casefold")?
        .extract()?;
    if PRODUCTION_TARGET_NAMES.contains(&target.as_str()) {
        return Ok(true);
    }
    let compiled_project: Value<'_> =
        getattr_or(analysis, "compiled_project", py.None().into_bound(py))?;
    getattr_or(&compiled_project, "production_target", object(py, false)?)?.is_truthy()
}

/// Build a frozen impact plan using only read-only adapter interactions.
#[pyfunction]
#[pyo3(signature = (*, request, analysis, connection, now = None, ttl = None, plan_id = None))]
fn plan_destruction<'py>(
    request: &Bound<'py, PyAny>,
    analysis: &Bound<'py, PyAny>,
    connection: &Bound<'py, PyAny>,
    now: Option<Bound<'py, PyAny>>,
    ttl: Option<Bound<'py, PyAny>>,
    plan_id: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = request.py();
    let ttl: Value<'py> = match ttl {
        Some(ttl) => ttl,
        None => py
            .import(CONSTANTS)?
            .getattr("DEFAULT_DESTRUCTION_PLAN_TTL")?,
    };
    let created_at: Value<'py> = validated_created_at(py, now, &ttl)?;
    let current: Vec<String> = current_pipeline_names(request, analysis)?;
    let pipeline_names: Vec<String> = request.getattr("pipeline_names")?.extract()?;
    let included_names: Vec<String> = request
        .getattr("included_dependent_pipeline_names")?
        .extract()?;
    let historical_names_requested: bool = pipeline_names
        .iter()
        .chain(&included_names)
        .any(|name| !current.contains(name));
    let include_orphans: bool = request.getattr("include_orphans")?.is_truthy()?;
    let historical_snapshot: Option<Value<'py>> = if include_orphans || historical_names_requested {
        Some(load_manifest_snapshot(request, analysis, connection)?)
    } else {
        None
    };
    let project: ProjectArgument = native_project(analysis)?;
    let database: String = request.getattr("database")?.extract()?;
    let native_request: RequestArgument = (
        request.getattr("operation")?.str()?.extract()?,
        database.clone(),
        pipeline_names,
        included_names,
        include_orphans,
    );
    let (requested, included, affected) =
        destruction_available_pipelines(current.clone(), historical_snapshot.as_ref())
            .and_then(|available| {
                resolve_destruction_selection(
                    py,
                    project.clone(),
                    native_request.clone(),
                    available,
                )
            })
            .or_else(|error| Err(planning_error(py, error)?))?;
    let connection_limit: Value<'py> = connection.call_method0("load_relation_drop_size_limit")?;
    let server_limit: Value<'py> =
        connection.call_method0("load_relation_drop_size_server_default")?;
    let size_override: Value<'py> = analysis
        .getattr("compiled_project")?
        .getattr("destruction_relation_drop_size_limit")?;
    let size_limit: Value<'py> = if size_override.is_none() {
        connection_limit
    } else {
        size_override.clone()
    };
    let catalog: Value<'py> = connection.call_method1("load_catalog", (&database,))?;
    let inventory: Value<'py> = connection.call_method1(
        "load_deployment_inventory",
        (request.getattr("metadata_database")?,),
    )?;
    let scoped: PyResult<ScopedEvidence> = (|| {
        let (models, sources, owned) = plan_destruction_scope(
            py,
            project,
            native_request,
            affected.clone(),
            current,
            historical_snapshot.as_ref(),
            &catalog,
            &inventory,
        )?;
        let names: Vec<String> = owned.iter().map(|relation| relation.0.clone()).collect();
        let stats = relation_stats(connection, &database, &names)?.extract()?;
        let evidence: Vec<EvidenceTuple> = build_destruction_evidence(
            py,
            &database,
            owned.clone(),
            &catalog,
            stats,
            size_limit.extract()?,
        )?;
        Ok((models, sources, owned, evidence))
    })();
    let (affected_models, affected_sources, owned, native_evidence) =
        scoped.or_else(|error| Err(planning_error(py, error)?))?;
    let relations: Vec<Value<'py>> = native_evidence
        .iter()
        .map(|native| relation_evidence(py, native))
        .collect::<PyResult<_>>()
        .or_else(|error| Err(planning_error(py, error)?))?;
    let owned_names: BTreeSet<String> = owned.iter().map(|relation| relation.0.clone()).collect();
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    keywords.set_item("database", &database)?;
    keywords.set_item("relation_names", PyTuple::new(py, &owned_names)?)?;
    let blocked: Value<'py> =
        connection.call_method("load_external_dependants", (), Some(&keywords))?;
    if blocked.is_truthy()? {
        let blocked: Bound<'py, PyList> =
            sorted(py, &py.get_type::<PySet>().call1((blocked,))?)?.cast_into()?;
        return Err(raise(
            py,
            "DestructionExternalDependencyError",
            blocked.to_tuple(),
        ));
    }
    check_drop_order(py, &native_evidence)?;
    let challenges: Vec<String> =
        py_build_destruction_challenges(py, affected.clone(), production_reset(request, analysis)?)
            .or_else(|error| Err(planning_error(py, error)?))?;
    let manifest: String = manifest_fingerprint(analysis)?;
    let include_orphans: bool = include_orphans || historical_names_requested;
    let limit: Option<i128> = size_limit.extract()?;
    let server_limit_value: Option<i128> = server_limit.extract()?;
    let override_value: Option<i128> = size_override.extract()?;
    let operation: String = request.getattr("operation")?.str()?.extract()?;
    let target: String = request.getattr("target")?.extract()?;
    let metadata_database: String = request.getattr("metadata_database")?.extract()?;
    let fingerprint: String = destruction_plan_fingerprint(
        &operation,
        &target,
        &database,
        &metadata_database,
        requested.clone(),
        included.clone(),
        affected.clone(),
        affected_models.clone(),
        affected_sources.clone(),
        native_evidence,
        challenges.clone(),
        &manifest,
        limit,
        server_limit_value,
        override_value,
        true,
        include_orphans,
    )?;
    let plan_id: Value<'py> = match plan_id.filter(|plan_id| plan_id.is_truthy().unwrap_or(true)) {
        Some(plan_id) => plan_id,
        None => object(
            py,
            format!(
                "destruction_{}",
                py.import("uuid")?
                    .call_method0("uuid4")?
                    .getattr("hex")?
                    .str()?
            ),
        )?,
    };
    let operation: Value<'py> = py
        .import(TYPES)?
        .getattr("DestructionOperation")?
        .call1((request.getattr("operation")?,))?;
    let expires_at: Value<'py> = created_at.add(&ttl)?;
    build(
        py,
        MODELS,
        "DestructionPlan",
        &[
            ("plan_id", plan_id),
            ("operation", operation),
            ("target", object(py, &target)?),
            ("database", object(py, &database)?),
            ("metadata_database", object(py, &metadata_database)?),
            ("requested_pipeline_names", string_tuple(py, &requested)?),
            (
                "included_dependent_pipeline_names",
                string_tuple(py, &included)?,
            ),
            ("affected_pipeline_names", string_tuple(py, &affected)?),
            ("affected_model_names", string_tuple(py, &affected_models)?),
            (
                "affected_source_names",
                string_tuple(py, &affected_sources)?,
            ),
            ("relations", object(py, PyTuple::new(py, relations)?)?),
            ("challenges", string_tuple(py, &challenges)?),
            ("preserves_sources", object(py, false)?),
            ("preserves_replay_data", object(py, false)?),
            ("manifest_fingerprint", object(py, &manifest)?),
            ("plan_fingerprint", object(py, fingerprint)?),
            ("created_at", created_at),
            ("expires_at", expires_at),
            ("relation_drop_size_limit", size_limit),
            ("relation_drop_size_server_limit", server_limit),
            ("relation_drop_size_override", size_override),
            ("relation_drop_size_policy_observed", object(py, true)?),
            ("include_orphans", object(py, include_orphans)?),
        ],
    )
}

/// List manifest-owned pipelines absent from the current compiled project.
#[pyfunction]
#[pyo3(signature = (*, request, analysis, connection))]
fn list_inactive_pipelines<'py>(
    request: &Bound<'py, PyAny>,
    analysis: &Bound<'py, PyAny>,
    connection: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py: Python<'py> = request.py();
    let current: Vec<String> = current_pipeline_names(request, analysis)?;
    let snapshot: Value<'py> = load_manifest_snapshot(request, analysis, connection)?;
    let pipelines: Vec<Value<'py>> = list_destruction_inactive_pipelines(current, &snapshot)?
        .into_iter()
        .map(|(name, model_count, resource_count, last_published_at)| {
            build(
                py,
                MODELS,
                "InactivePipeline",
                &[
                    ("name", object(py, name)?),
                    ("model_count", object(py, model_count)?),
                    ("resource_count", object(py, resource_count)?),
                    ("last_published_at", object(py, last_published_at)?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, pipelines)
}

/// Stable, ordered challenge values for the affected pipeline set.
#[pyfunction]
#[pyo3(signature = (*, pipeline_names, production_reset = false))]
fn build_destruction_challenges<'py>(
    py: Python<'py>,
    pipeline_names: Vec<String>,
    production_reset: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let challenges: Vec<String> =
        py_build_destruction_challenges(py, pipeline_names, production_reset)
            .or_else(|error| Err(planning_error(py, error)?))?;
    PyTuple::new(py, challenges)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_destruction, module)?)?;
    module.add_function(wrap_pyfunction!(list_inactive_pipelines, module)?)?;
    module.add_function(wrap_pyfunction!(build_destruction_challenges, module)?)
}
