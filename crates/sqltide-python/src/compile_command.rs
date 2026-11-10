//! `sqt compile`: analyze the project, build every static compile artifact in memory (rendered
//! resources with redacted source credentials, model queries, audits, SQL tests, the manifest and
//! the DAG), then stage them and replace the target's static owners, restoring the previous
//! owners if a replacement fails. Filesystem operations go through Python's `os`, `shutil`,
//! `tempfile` and `pathlib` so their errors are the ones Python raised before.

use std::path::PathBuf;

use pyo3::exceptions::{PyAttributeError, PyOSError, PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyString};
use sqltide_compile::paths::{self, ModelResource, UnsafeSegment};
use sqltide_compile::staging::{StagingError, stage_files};

use crate::artifacts::json_dumps;
use crate::build_cli::formatted;
use crate::build_command::print;
use crate::direct_build::{
    Value, boolean, call, class, is_a, items, method, none, or, replace, text, tuple,
};
use crate::handling::while_handling;
use crate::model_sql::chained;

const NATIVE: &str = "sqltide._native";
const COMPILE_EXCEPTIONS: &str = "sqltide.cli.compile.exceptions";
const CONTRACT_EXCEPTIONS: &str = "sqltide.compiler.compile.exceptions";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const RENDER_ERROR: &str = "sqltide.diagnostics.main.render_error";
const ARTIFACTS_FIELD: &str = "artifacts";
const STATIC_OWNERS: [&str; 3] = ["compiled", "manifest.json", "sqltide_dag.json"];
const RESERVED_OWNERS: [&str; 4] = ["compiled", "run", "manifest.json", "sqltide_dag.json"];
const BROKER_USERINFO_SEPARATOR: &str = "@";
const SENSITIVE_SETTING_FRAGMENTS: [&str; 6] = [
    "credential",
    "password",
    "private_key",
    "sasl",
    "secret",
    "token",
];

/// One `(target-relative path, contents)` static artifact.
type ArtifactFile = (String, String);

/// `path.mkdir(parents=True, exist_ok=True)`.
fn make_directories(path: &Value<'_>) -> PyResult<()> {
    let py: Python<'_> = path.py();
    method(
        path,
        "mkdir",
        &[
            ("parents", boolean(py, true)?),
            ("exist_ok", boolean(py, true)?),
        ],
    )?;
    Ok(())
}

/// `shutil.rmtree(path, ignore_errors=True)`.
fn remove_tree(path: &Value<'_>) -> PyResult<()> {
    let py: Python<'_> = path.py();
    let arguments: Bound<'_, PyDict> = PyDict::new(py);
    arguments.set_item("ignore_errors", true)?;
    py.import("shutil")?
        .getattr("rmtree")?
        .call((path,), Some(&arguments))?;
    Ok(())
}

fn truthy(value: &Value<'_>, name: &str) -> PyResult<bool> {
    value.call_method0(name)?.is_truthy()
}

/// `getattr(value, name, None)`.
fn attribute_or_none<'py>(value: &Value<'py>, name: &str) -> PyResult<Value<'py>> {
    match value.getattr(name) {
        Ok(found) => Ok(found),
        Err(error) if error.is_instance_of::<PyAttributeError>(value.py()) => Ok(none(value.py())),
        Err(error) => Err(error),
    }
}

/// `raise CompileArtifactError(message) from None` inside `except ValueError`.
fn artifact_error_from_none(py: Python<'_>, message: String) -> PyErr {
    let raise = || -> PyResult<PyErr> {
        let context: Value<'_> = PyValueError::new_err(message.clone())
            .into_value(py)
            .into_bound(py)
            .into_any();
        let error: Value<'_> =
            class(py, COMPILE_EXCEPTIONS, "CompileArtifactError")?.call1((message,))?;
        chained(&error, &context, None)?;
        Ok(PyErr::from_value(error))
    };
    raise().unwrap_or_else(|error| error)
}

fn unsafe_segment(py: Python<'_>, error: UnsafeSegment) -> PyErr {
    artifact_error_from_none(py, error.to_string())
}

/// `sql.rstrip() + "\n"`.
fn normalized<'py>(sql: &Value<'py>) -> PyResult<Value<'py>> {
    sql.call_method0("rstrip")?.add("\n")
}

// Artifact paths.

fn resource_kind(resource: &Value<'_>) -> PyResult<&'static str> {
    Ok(if is_a(resource, ADAPTER_MODELS, "AdapterManagedSource")? {
        "managed_source"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterTable")? {
        "table"
    } else if is_a(resource, ADAPTER_MODELS, "AdapterView")? {
        "view"
    } else {
        "materialized_view"
    })
}

fn model_query_path(py: Python<'_>, pipeline: &Value<'_>, model: &Value<'_>) -> PyResult<String> {
    let pipeline: String = pipeline.extract()?;
    let model: String = model.extract()?;
    paths::model_query_path(&pipeline, &model).map_err(|error| unsafe_segment(py, error))
}

fn model_resource_path(
    py: Python<'_>,
    pipeline: &Value<'_>,
    model: &Value<'_>,
    resource: &Value<'_>,
) -> PyResult<String> {
    let pipeline: String = pipeline.extract()?;
    let model: String = model.extract()?;
    let kind: ModelResource = match resource_kind(resource)? {
        "table" => ModelResource::Table,
        "view" => ModelResource::View,
        "materialized_view" => ModelResource::MaterializedView,
        other => {
            return Err(artifact_error_from_none(
                py,
                format!("Unknown model resource kind '{other}'"),
            ));
        }
    };
    paths::model_resource_path(&pipeline, &model, kind).map_err(|error| unsafe_segment(py, error))
}

fn source_resource_path(
    py: Python<'_>,
    source: &Value<'_>,
    resource: &Value<'_>,
) -> PyResult<String> {
    let source: String = source.extract()?;
    let resource: String = resource.extract()?;
    paths::source_resource_path(&source, &resource).map_err(|error| unsafe_segment(py, error))
}

/// An audit file relative to the project's `audits` folder, else to the project.
fn audit_relative_path<'py>(
    py: Python<'py>,
    file_path: &Value<'py>,
    project_dir: &Value<'py>,
) -> PyResult<Value<'py>> {
    let resolved_file: Value<'py> = file_path.call_method0("resolve")?;
    let resolved_project: Value<'py> = project_dir.call_method0("resolve")?;
    let audits_root: Value<'py> = resolved_project.div("audits")?;
    let first: PyErr = match resolved_file.call_method1("relative_to", (&audits_root,)) {
        Ok(relative) => return Ok(relative),
        Err(error) if error.is_instance_of::<PyValueError>(py) => error,
        Err(error) => return Err(error),
    };
    match resolved_file.call_method1("relative_to", (&resolved_project,)) {
        Ok(relative) => Ok(relative),
        Err(second) if second.is_instance_of::<PyValueError>(py) => {
            let second: PyErr = while_handling(py, second, &first);
            let cause: Value<'py> = second.value(py).clone().into_any();
            let raise = || -> PyResult<PyErr> {
                let message: String = format!(
                    "Audit file '{}' is outside project root '{}'",
                    formatted(file_path)?,
                    formatted(project_dir)?
                );
                let error: Value<'py> =
                    class(py, COMPILE_EXCEPTIONS, "CompileArtifactError")?.call1((message,))?;
                chained(&error, &cause, Some(&cause))?;
                Ok(PyErr::from_value(error))
            };
            Err(match raise() {
                Ok(error) => error,
                Err(failure) => while_handling(py, failure, &second),
            })
        }
        Err(second) => Err(while_handling(py, second, &first)),
    }
}

fn audit_path(py: Python<'_>, audit: &Value<'_>, project_dir: &Value<'_>) -> PyResult<String> {
    let relative_file: String = audit_relative_path(py, &audit.getattr("file_path")?, project_dir)?
        .call_method0("as_posix")?
        .extract()?;
    let name: Option<String> = audit.getattr("name")?.extract()?;
    paths::audit_path(&relative_file, name.as_deref()).map_err(|error| unsafe_segment(py, error))
}

/// `resource.name or resource.file_path.stem`.
fn identity<'py>(resource: &Value<'py>) -> PyResult<Value<'py>> {
    let name: Value<'py> = resource.getattr("name")?;
    if name.is_truthy()? {
        return Ok(name);
    }
    resource.getattr("file_path")?.getattr("stem")
}

fn test_targets<'py>(test_case: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    items(&test_case.getattr("target_cases")?)?
        .iter()
        .map(|target| target.getattr("target_model_name"))
        .collect()
}

fn test_path(py: Python<'_>, test_case: &Value<'_>) -> PyResult<String> {
    let name: String = identity(test_case)?.extract()?;
    let targets: Vec<String> = test_targets(test_case)?
        .iter()
        .map(|target| target.extract())
        .collect::<PyResult<_>>()?;
    paths::static_test_path(&name, targets.iter().map(String::as_str))
        .map_err(|error| unsafe_segment(py, error))
}

// Static artifacts.

/// A static-artifact copy of a managed source with credential-bearing values removed.
fn redacted_managed_source<'py>(py: Python<'py>, resource: &Value<'py>) -> PyResult<Value<'py>> {
    let placeholder: Value<'py> = py
        .import(ADAPTER_CONSTANTS)?
        .getattr("REDACTED_SECRET_PLACEHOLDER")?;
    let broker_list: Value<'py> = resource.getattr("broker_list")?;
    let broker_list: Value<'py> = if broker_list.contains(BROKER_USERINFO_SEPARATOR)? {
        placeholder.clone()
    } else {
        broker_list
    };
    let mut settings: Vec<Value<'py>> = Vec::new();
    for setting in items(&resource.getattr("settings")?)? {
        let (key, value): (Value<'py>, Value<'py>) = setting.extract()?;
        let lowered: String = key.call_method0("lower")?.extract()?;
        let sensitive: bool = SENSITIVE_SETTING_FRAGMENTS
            .iter()
            .any(|fragment| lowered.contains(fragment));
        let rendered: Value<'py> = if sensitive {
            placeholder.clone()
        } else {
            value
        };
        settings.push(tuple(py, vec![key, rendered])?);
    }
    replace(
        resource,
        &[
            ("broker_list", broker_list),
            ("settings", tuple(py, settings)?),
        ],
    )
}

/// A pipeline's project database, else the target's, else `default`.
fn pipeline_database<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
    pipeline: &Value<'py>,
) -> PyResult<Value<'py>> {
    let project: Value<'py> = pipeline.getattr("project")?;
    if !project.is_none() {
        let database: Value<'py> = project.getattr("default_database")?;
        if !database.is_none() {
            return Ok(database);
        }
    }
    or(
        analysis
            .getattr("compile_inputs")?
            .getattr("effective_target")?
            .getattr("default_database")?,
        &text(py, "default"),
    )
}

/// Every logical key's `(resource, normalized rendered SQL)` pairs.
fn rendered_resources_by_logical_key<'py>(
    py: Python<'py>,
    analysis: &Value<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let database_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for pipeline in items(&analysis.getattr("compiled_project")?.getattr("pipelines")?)? {
        let database: Value<'py> = pipeline_database(py, analysis, &pipeline)?;
        let source: Value<'py> = pipeline.getattr("source")?;
        if !source.is_none() {
            database_by_key.call_method1("setdefault", (source.getattr("key")?, &database))?;
        }
        for model in items(&pipeline.getattr("models")?)? {
            database_by_key.set_item(model.getattr("key")?, &database)?;
        }
    }
    let profile: Value<'py> = analysis.getattr("adapter_profile")?;
    let rendered_by_key: Bound<'py, PyDict> = PyDict::new(py);
    let resources_by_key: Value<'py> = analysis
        .getattr("realized_project")?
        .getattr("resources_by_logical_key")?;
    for entry in resources_by_key.call_method0("items")?.try_iter()? {
        let (key, resources): (Value<'py>, Value<'py>) = entry?.extract()?;
        let database: Value<'py> = database_by_key.as_any().get_item(&key)?;
        let mut rendered: Vec<Value<'py>> = Vec::new();
        for resource in items(&resources)? {
            let target: Value<'py> = if is_a(&resource, ADAPTER_MODELS, "AdapterManagedSource")? {
                redacted_managed_source(py, &resource)?
            } else {
                resource.clone()
            };
            let sql: Value<'py> = method(
                &profile,
                "render_resource",
                &[("resource", target), ("database", database.clone())],
            )?;
            rendered.push(tuple(py, vec![resource, normalized(&sql)?])?);
        }
        rendered_by_key.set_item(key, tuple(py, rendered)?)?;
    }
    Ok(rendered_by_key)
}

fn rendered_pairs<'py>(
    rendered_by_key: &Bound<'py, PyDict>,
    key: &Value<'py>,
) -> PyResult<Vec<(Value<'py>, Value<'py>)>> {
    items(&rendered_by_key.as_any().get_item(key)?)?
        .iter()
        .map(|pair| pair.extract())
        .collect()
}

/// Every compiled file, sorted by path: source resources, model queries and resources, audits
/// and SQL tests.
fn compiled_files(py: Python<'_>, analysis: &Value<'_>) -> PyResult<Vec<ArtifactFile>> {
    let rendered_by_key: Bound<'_, PyDict> = rendered_resources_by_logical_key(py, analysis)?;
    let project: Value<'_> = analysis.getattr("compiled_project")?;
    let realized: Value<'_> = analysis.getattr("realized_project")?;
    let mut files: Vec<ArtifactFile> = Vec::new();
    for source in items(&project.getattr("sources")?)? {
        let key: Value<'_> = source.getattr("key")?;
        for (resource, sql) in rendered_pairs(&rendered_by_key, &key)? {
            let path: String =
                source_resource_path(py, &key.getattr("name")?, &resource.getattr("name")?)?;
            files.push((path, sql.extract()?));
        }
    }
    for model in items(&project.getattr("models")?)? {
        let key: Value<'_> = model.getattr("key")?;
        let pipeline: Value<'_> = model.getattr("pipeline_name")?;
        let name: Value<'_> = key.getattr("name")?;
        let path: String = model_query_path(py, &pipeline, &name)?;
        let query: Value<'_> = realized
            .getattr("resolved_query_by_model_key")?
            .get_item(&key)?;
        files.push((path, normalized(&query)?.extract()?));
        for (resource, sql) in rendered_pairs(&rendered_by_key, &key)? {
            let pipeline: Value<'_> = model.getattr("pipeline_name")?;
            let name: Value<'_> = model.getattr("key")?.getattr("name")?;
            let path: String = model_resource_path(py, &pipeline, &name, &resource)?;
            files.push((path, sql.extract()?));
        }
    }
    for audit in items(&project.getattr("audits")?)? {
        let project_dir: Value<'_> = analysis
            .getattr("discovered_inputs")?
            .getattr("project_dir")?;
        let path: String = audit_path(py, &audit, &project_dir)?;
        files.push((path, normalized(&audit.getattr("query")?)?.extract()?));
    }
    for test_case in items(&project.getattr("test_cases")?)? {
        let path: String = test_path(py, &test_case)?;
        files.push((path, normalized(&test_case.getattr("query")?)?.extract()?));
    }
    files.sort_by(|left, right| left.0.cmp(&right.0));
    validate_unique_paths(py, files.iter().map(|file| file.0.as_str()))?;
    Ok(files)
}

fn validate_unique_paths<'a>(
    py: Python<'_>,
    paths: impl IntoIterator<Item = &'a str>,
) -> PyResult<()> {
    let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
    for path in paths {
        if !seen.insert(path) {
            return Err(PyErr::from_value(
                class(py, COMPILE_EXCEPTIONS, "CompileArtifactError")?
                    .call1((format!("Multiple compile artifacts resolve to '{path}'"),))?,
            ));
        }
    }
    Ok(())
}

// The manifest.

fn dict<'py>(py: Python<'py>, pairs: Vec<(&str, Value<'py>)>) -> PyResult<Value<'py>> {
    let dict: Bound<'py, PyDict> = PyDict::new(py);
    for (key, value) in pairs {
        dict.set_item(key, value)?;
    }
    Ok(dict.into_any())
}

fn owned<'py>(py: Python<'py>, value: &str) -> Value<'py> {
    PyString::new(py, value).into_any()
}

fn tuple_of<'py>(py: Python<'py>, value: &Value<'py>) -> PyResult<Value<'py>> {
    tuple(py, items(value)?)
}

/// A path relative to the project, else as written.
fn project_relative_path<'py>(
    py: Python<'py>,
    path: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let relative = || -> PyResult<Value<'py>> {
        let resolved: Value<'py> = path.call_method0("resolve")?;
        let project_dir: Value<'py> = analysis
            .getattr("discovered_inputs")?
            .getattr("project_dir")?
            .call_method0("resolve")?;
        resolved
            .call_method1("relative_to", (project_dir,))?
            .call_method0("as_posix")
    };
    match relative() {
        Ok(relative) => Ok(relative),
        Err(error) if error.is_instance_of::<PyValueError>(py) => path
            .call_method0("as_posix")
            .map_err(|failure| while_handling(py, failure, &error)),
        Err(error) => Err(error),
    }
}

fn resource_entries<'py>(
    py: Python<'py>,
    resources: &[Value<'py>],
    path: impl Fn(&Value<'py>) -> PyResult<String>,
) -> PyResult<Value<'py>> {
    let mut entries: Vec<Value<'py>> = Vec::new();
    for resource in resources {
        entries.push(dict(
            py,
            vec![
                ("kind", owned(py, resource_kind(resource)?)),
                ("name", resource.getattr("name")?),
                ("path", owned(py, &path(resource)?)),
            ],
        )?);
    }
    tuple(py, entries)
}

fn pipeline_entry<'py>(
    py: Python<'py>,
    pipeline: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let source: Value<'py> = pipeline.getattr("source")?;
    let mut logical_keys: Vec<Value<'py>> = Vec::new();
    if !source.is_none() {
        logical_keys.push(source.getattr("key")?);
    }
    let models: Vec<Value<'py>> = items(&pipeline.getattr("models")?)?;
    for model in &models {
        logical_keys.push(model.getattr("key")?);
    }
    let file: Value<'py> = project_relative_path(py, &pipeline.getattr("file_path")?, analysis)?;
    let mut names: Vec<Value<'py>> = Vec::new();
    for model in &models {
        names.push(model.getattr("key")?.getattr("name")?);
    }
    let relation_names: Value<'py> = analysis
        .getattr("realized_project")?
        .getattr("relation_name_by_logical_key")?;
    let relations: Bound<'py, PyDict> = PyDict::new(py);
    for key in &logical_keys {
        relations.set_item(key.getattr("name")?, relation_names.get_item(key)?)?;
    }
    dict(
        py,
        vec![
            ("file", file),
            ("models", tuple(py, names)?),
            ("name", pipeline.getattr("pipeline")?.getattr("name")?),
            (
                "replay_lineage_mode",
                pipeline.getattr("effective_replay_lineage_mode")?,
            ),
            ("relations", relations.into_any()),
            (
                "resolved_database",
                pipeline_database(py, analysis, pipeline)?,
            ),
            (
                "source_name",
                if source.is_none() {
                    none(py)
                } else {
                    source.getattr("key")?.getattr("name")?
                },
            ),
        ],
    )
}

fn source_retention_entry<'py>(py: Python<'py>, source: &Value<'py>) -> PyResult<Value<'py>> {
    let step: Value<'py> = source.getattr("source")?;
    if !is_a(&step, DISCOVERY_MODELS, "KafkaLandingStep")? {
        return Ok(none(py));
    }
    let kafka: Value<'py> = step.getattr("kafka")?;
    let value: Value<'py> = kafka.getattr("retention")?;
    let origin: Value<'py> = kafka.getattr("retention_origin")?;
    if origin.is_none() {
        return Ok(none(py));
    }
    if value.is(boolean(py, false)?) {
        return dict(
            py,
            vec![
                ("origin", origin.str()?.into_any()),
                ("status", owned(py, "disabled")),
            ],
        );
    }
    if !is_a(&value, DISCOVERY_MODELS, "KafkaRetentionPolicy")? {
        return Ok(none(py));
    }
    let cap_at: Value<'py> = value.getattr("cap_at")?;
    dict(
        py,
        vec![
            ("origin", origin.str()?.into_any()),
            ("status", owned(py, "applied")),
            ("duration_seconds", value.getattr("duration_seconds")?),
            ("timestamp", value.getattr("timestamp")?.str()?.into_any()),
            ("fallback", value.getattr("fallback")?.str()?.into_any()),
            (
                "cap_at",
                if cap_at.is_none() {
                    none(py)
                } else {
                    cap_at.str()?.into_any()
                },
            ),
        ],
    )
}

fn source_entry<'py>(
    py: Python<'py>,
    source: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let step: Value<'py> = source.getattr("source")?;
    let naming_macro: Value<'py> = attribute_or_none(&step, "naming_macro")?;
    let key: Value<'py> = source.getattr("key")?;
    let realized: Value<'py> = analysis.getattr("realized_project")?;
    let resources: Vec<Value<'py>> = items(
        &realized
            .getattr("resources_by_logical_key")?
            .get_item(&key)?,
    )?;
    let mut landing_table: Value<'py> = none(py);
    for resource in &resources {
        if is_a(resource, ADAPTER_MODELS, "AdapterTable")? {
            landing_table = resource.clone();
            break;
        }
    }
    let name: Value<'py> = key.getattr("name")?;
    let name_origin: Value<'py> = dict(
        py,
        vec![
            ("kind", step.getattr("name_origin")?),
            ("macro", naming_macro),
            (
                "macro_fingerprint",
                attribute_or_none(&step, "naming_macro_fingerprint")?,
            ),
        ],
    )?;
    let ttl: Value<'py> = if landing_table.is_none() {
        none(py)
    } else {
        landing_table.getattr("ttl")?
    };
    dict(
        py,
        vec![
            (
                "logical_key",
                owned(py, &format!("source:{}", formatted(&name)?)),
            ),
            ("name", name.clone()),
            ("name_origin", name_origin),
            (
                "relation_name",
                realized
                    .getattr("relation_name_by_logical_key")?
                    .get_item(&key)?,
            ),
            (
                "replay_lineage_mode",
                source.getattr("effective_replay_lineage_mode")?,
            ),
            ("retention", source_retention_entry(py, source)?),
            ("ttl", ttl),
            (
                "resources",
                resource_entries(py, &resources, |resource| {
                    source_resource_path(py, &name, &resource.getattr("name")?)
                })?,
            ),
        ],
    )
}

fn model_retention_entry<'py>(py: Python<'py>, model: &Value<'py>) -> PyResult<Value<'py>> {
    let retention: Value<'py> = model.getattr("retention")?;
    let value: Value<'py> = retention.getattr("value")?;
    let origin: Value<'py> = retention.getattr("origin")?;
    if origin.is_none() {
        return Ok(none(py));
    }
    if value.is(boolean(py, false)?) {
        return dict(
            py,
            vec![
                ("origin", origin.str()?.into_any()),
                ("status", owned(py, "disabled")),
            ],
        );
    }
    if !is_a(&value, DISCOVERY_MODELS, "ModelRetentionPolicy")? {
        return Ok(none(py));
    }
    let status: &str = if model.getattr("retention_applied")?.is_truthy()? {
        "applied"
    } else {
        "skipped"
    };
    dict(
        py,
        vec![
            ("origin", origin.str()?.into_any()),
            ("status", owned(py, status)),
            ("duration_seconds", value.getattr("duration_seconds")?),
            ("timestamp_column", value.getattr("timestamp_column")?),
            ("cap_at_column", value.getattr("cap_at_column")?),
            (
                "when_missing",
                value.getattr("when_missing")?.str()?.into_any(),
            ),
        ],
    )
}

fn model_entry<'py>(
    py: Python<'py>,
    model: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let key: Value<'py> = model.getattr("key")?;
    let realized: Value<'py> = analysis.getattr("realized_project")?;
    let resources: Vec<Value<'py>> = items(
        &realized
            .getattr("resources_by_logical_key")?
            .get_item(&key)?,
    )?;
    let name: Value<'py> = key.getattr("name")?;
    let pipeline: Value<'py> = model.getattr("pipeline_name")?;
    let entry: Bound<'py, PyDict> = PyDict::new(py);
    entry.set_item("logical_key", format!("model:{}", formatted(&name)?))?;
    entry.set_item("name", &name)?;
    entry.set_item("pipeline", &pipeline)?;
    entry.set_item("query_path", model_query_path(py, &pipeline, &name)?)?;
    entry.set_item("refs", tuple_of(py, &model.getattr("refs")?)?)?;
    entry.set_item(
        "relation_name",
        realized
            .getattr("relation_name_by_logical_key")?
            .get_item(&key)?,
    )?;
    entry.set_item(
        "resources",
        resource_entries(py, &resources, |resource| {
            let pipeline: Value<'py> = model.getattr("pipeline_name")?;
            let name: Value<'py> = model.getattr("key")?.getattr("name")?;
            model_resource_path(py, &pipeline, &name, resource)
        })?,
    )?;
    if is_a(model, COMPILE_MODELS, "CompiledViewModel")? {
        entry.set_item("source", none(py))?;
        entry.set_item("spec", none(py))?;
        return Ok(entry.into_any());
    }
    let mut table: Option<Value<'py>> = None;
    for resource in &resources {
        if is_a(resource, ADAPTER_MODELS, "AdapterTable")? {
            table = Some(resource.clone());
            break;
        }
    }
    let table: Value<'py> = table.ok_or_else(|| PyStopIteration::new_err(()))?;
    let mut columns: Vec<Value<'py>> = Vec::new();
    for column in items(&table.getattr("columns")?)? {
        columns.push(dict(
            py,
            vec![
                ("default", column.getattr("default_expression")?),
                ("name", column.getattr("name")?),
                ("type", column.getattr("type")?),
            ],
        )?);
    }
    let settings: Value<'py> = table.getattr("settings")?;
    let settings: Value<'py> = if settings.is_truthy()? {
        py.import("builtins")?.getattr("dict")?.call1((settings,))?
    } else {
        none(py)
    };
    let spec: Value<'py> = dict(
        py,
        vec![
            ("columns", tuple(py, columns)?),
            ("engine", table.getattr("engine")?),
            ("order_by", tuple_of(py, &table.getattr("order_by")?)?),
            ("partition_by", table.getattr("partition_by")?),
            ("settings", settings),
            ("ttl", table.getattr("ttl")?),
        ],
    )?;
    entry.set_item("source", model.getattr("transform")?.getattr("source")?)?;
    entry.set_item("retention", model_retention_entry(py, model)?)?;
    entry.set_item("spec", spec)?;
    Ok(entry.into_any())
}

fn audit_entry<'py>(
    py: Python<'py>,
    audit: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    let project_dir: Value<'py> = analysis
        .getattr("discovered_inputs")?
        .getattr("project_dir")?;
    dict(
        py,
        vec![
            (
                "file",
                project_relative_path(py, &audit.getattr("file_path")?, analysis)?,
            ),
            ("name", identity(audit)?),
            ("path", owned(py, &audit_path(py, audit, &project_dir)?)),
            (
                "referenced_models",
                tuple_of(py, &audit.getattr("referenced_model_names")?)?,
            ),
            ("severity", audit.getattr("severity")?),
        ],
    )
}

fn test_entry<'py>(
    py: Python<'py>,
    test_case: &Value<'py>,
    analysis: &Value<'py>,
) -> PyResult<Value<'py>> {
    dict(
        py,
        vec![
            (
                "file",
                project_relative_path(py, &test_case.getattr("file_path")?, analysis)?,
            ),
            ("name", identity(test_case)?),
            ("path", owned(py, &test_path(py, test_case)?)),
            ("targets", tuple(py, test_targets(test_case)?)?),
        ],
    )
}

/// The complete deterministic SQLTide-native manifest.
fn manifest_json(py: Python<'_>, analysis: &Value<'_>, files: &[ArtifactFile]) -> PyResult<String> {
    let project: Value<'_> = analysis.getattr("compiled_project")?;
    let payload: Bound<'_, PyDict> = PyDict::new(py);
    let mut artifacts: Vec<Value<'_>> =
        vec![owned(py, "manifest.json"), owned(py, "sqltide_dag.json")];
    artifacts.extend(files.iter().map(|file| owned(py, &file.0)));
    payload.set_item(ARTIFACTS_FIELD, tuple(py, artifacts)?)?;
    let audits: Bound<'_, PyDict> = PyDict::new(py);
    for audit in items(&project.getattr("audits")?)? {
        audits.set_item(identity(&audit)?, audit_entry(py, &audit, analysis)?)?;
    }
    payload.set_item("audits", audits)?;
    payload.set_item(
        "metadata",
        dict(
            py,
            vec![
                ("manifest_version", 1_i64.into_pyobject(py)?.into_any()),
                ("tool", owned(py, "sqltide")),
            ],
        )?,
    )?;
    let mut references: Vec<Value<'_>> = Vec::new();
    for reference in items(&project.getattr("allowed_cross_pipeline_references")?)? {
        references.push(dict(
            py,
            vec![
                ("upstream_pipeline", reference.getattr("upstream_pipeline")?),
                (
                    "downstream_pipeline",
                    reference.getattr("downstream_pipeline")?,
                ),
            ],
        )?);
    }
    payload.set_item(
        "dependencies",
        dict(
            py,
            vec![
                (
                    "model_reference_scope",
                    project.getattr("model_reference_scope")?.str()?.into_any(),
                ),
                ("allowed_cross_pipeline_references", tuple(py, references)?),
            ],
        )?,
    )?;
    let macros: Bound<'_, PyDict> = PyDict::new(py);
    let registry: Value<'_> = analysis
        .getattr("compile_inputs")?
        .getattr("macro_registry")?
        .getattr("macros")?;
    for entry in registry.call_method0("items")?.try_iter()? {
        let (name, registered): (Value<'_>, Value<'_>) = entry?.extract()?;
        macros.set_item(
            name,
            dict(
                py,
                vec![
                    (
                        "file",
                        registered
                            .getattr("relative_path")?
                            .call_method0("as_posix")?,
                    ),
                    ("source", registered.getattr("source")?),
                ],
            )?,
        )?;
    }
    payload.set_item("macros", macros)?;
    let models: Bound<'_, PyDict> = PyDict::new(py);
    for model in items(&project.getattr("models")?)? {
        models.set_item(
            model.getattr("key")?.getattr("name")?,
            model_entry(py, &model, analysis)?,
        )?;
    }
    payload.set_item("models", models)?;
    let pipelines: Bound<'_, PyDict> = PyDict::new(py);
    for pipeline in items(&project.getattr("pipelines")?)? {
        pipelines.set_item(
            pipeline.getattr("pipeline")?.getattr("name")?,
            pipeline_entry(py, &pipeline, analysis)?,
        )?;
    }
    payload.set_item("pipelines", pipelines)?;
    let sources: Bound<'_, PyDict> = PyDict::new(py);
    for source in items(&project.getattr("sources")?)? {
        sources.set_item(
            source.getattr("key")?.getattr("name")?,
            source_entry(py, &source, analysis)?,
        )?;
    }
    payload.set_item("sources", sources)?;
    let tests: Bound<'_, PyDict> = PyDict::new(py);
    for test_case in items(&project.getattr("test_cases")?)? {
        tests.set_item(identity(&test_case)?, test_entry(py, &test_case, analysis)?)?;
    }
    payload.set_item("tests", tests)?;
    Ok(json_dumps(payload.as_any(), Some(2), true)? + "\n")
}

// Publication.

/// Write the compiled files, manifest and DAG under a fresh staging root.
fn stage(
    py: Python<'_>,
    staging_root: &Value<'_>,
    files: &[ArtifactFile],
    manifest_json: &str,
    dag_json: &str,
) -> PyResult<()> {
    make_directories(&staging_root.div("compiled")?)?;
    let root: PathBuf = staging_root.extract()?;
    py.detach(|| stage_files(&root, files))
        .map_err(|error| match error {
            StagingError::Escapes(_) => artifact_error_from_none(py, error.to_string()),
            StagingError::Io(error) => PyErr::from(error),
        })?;
    for (owner, contents) in [
        ("manifest.json", manifest_json),
        ("sqltide_dag.json", dag_json),
    ] {
        method(
            &staging_root.div(owner)?,
            "write_text",
            &[
                ("data", owned(py, contents)),
                ("encoding", owned(py, "utf-8")),
            ],
        )?;
    }
    Ok(())
}

/// Put back the previous static owners after a failed replacement.
fn restore_previous_target(
    py: Python<'_>,
    backup_root: &Value<'_>,
    target_dir: &Value<'_>,
    moved: &[&str],
    published: &[&str],
) -> PyResult<()> {
    for owner in published.iter().rev() {
        let path: Value<'_> = target_dir.div(*owner)?;
        if truthy(&path, "is_dir")? {
            py.import("shutil")?.getattr("rmtree")?.call1((&path,))?;
        } else if truthy(&path, "exists")? {
            path.call_method0("unlink")?;
        }
    }
    let replace_path: Value<'_> = py.import("os")?.getattr("replace")?;
    for owner in moved.iter().rev() {
        replace_path.call1((backup_root.div(*owner)?, target_dir.div(*owner)?))?;
    }
    Ok(())
}

/// Replace each static owner, moving the previous one to the backup root first.
fn publish_staged_target(
    py: Python<'_>,
    staging_root: &Value<'_>,
    backup_root: &Value<'_>,
    target_dir: &Value<'_>,
) -> PyResult<()> {
    make_directories(target_dir)?;
    let replace_path: Value<'_> = py.import("os")?.getattr("replace")?;
    let mut moved: Vec<&str> = Vec::new();
    let mut published: Vec<&str> = Vec::new();
    let mut replace_owners = || -> PyResult<()> {
        for owner in STATIC_OWNERS {
            let live_path: Value<'_> = target_dir.div(owner)?;
            if truthy(&live_path, "exists")? {
                replace_path.call1((&live_path, backup_root.div(owner)?))?;
                moved.push(owner);
            }
            replace_path.call1((staging_root.div(owner)?, &live_path))?;
            published.push(owner);
        }
        Ok(())
    };
    match replace_owners() {
        Ok(()) => Ok(()),
        Err(error) if error.is_instance_of::<PyOSError>(py) => {
            restore_previous_target(py, backup_root, target_dir, &moved, &published)
                .map_err(|failure| while_handling(py, failure, &error))?;
            Err(error)
        }
        Err(error) => Err(error),
    }
}

/// Pipeline folders an older target layout kept beside the static owners.
fn legacy_pipeline_paths<'py>(
    py: Python<'py>,
    target_dir: &Value<'py>,
) -> PyResult<Vec<Value<'py>>> {
    let manifest_path: Value<'py> = target_dir.div("manifest.json")?;
    if !truthy(&manifest_path, "is_file")? {
        return Ok(Vec::new());
    }
    let contents: Value<'py> = method(
        &manifest_path,
        "read_text",
        &[("encoding", owned(py, "utf-8"))],
    )?;
    let manifest: Value<'py> = py.import("json")?.getattr("loads")?.call1((contents,))?;
    if manifest.contains(ARTIFACTS_FIELD)? {
        return Ok(Vec::new());
    }
    let pipelines: Value<'py> = manifest.call_method1("get", ("pipelines", PyDict::new(py)))?;
    let reserved: Bound<'py, PyFrozenSet> = PyFrozenSet::new(py, RESERVED_OWNERS)?;
    let mut legacy: Vec<Value<'py>> = Vec::new();
    for pipeline_name in pipelines.try_iter()? {
        let pipeline_name: Value<'py> = pipeline_name?;
        let pipeline_path: Value<'py> = target_dir.div(&pipeline_name)?;
        if reserved.contains(&pipeline_name)? || !truthy(&pipeline_path, "is_dir")? {
            continue;
        }
        legacy.push(pipeline_path);
    }
    Ok(legacy)
}

/// Stage every static owner before replacing the live compiled target.
fn publish_static_compile_artifacts(
    py: Python<'_>,
    files: &[ArtifactFile],
    manifest_json: &str,
    dag_json: &str,
    target_dir: &Value<'_>,
) -> PyResult<()> {
    let parent: Value<'_> = target_dir.getattr("parent")?;
    make_directories(&parent)?;
    let path_class: Value<'_> = py.import("pathlib")?.getattr("Path")?;
    let tempfile: Value<'_> = py.import("tempfile")?.into_any();
    let temporary = |prefix: &str| -> PyResult<Value<'_>> {
        path_class.call1((method(
            &tempfile,
            "mkdtemp",
            &[("prefix", owned(py, prefix)), ("dir", parent.clone())],
        )?,))
    };
    let staging_root: Value<'_> = temporary(".sqltide-static-staging-")?;
    let backup_root: Value<'_> = temporary(".sqltide-static-backup-")?;
    let legacy: Vec<Value<'_>> = legacy_pipeline_paths(py, target_dir)?;
    let outcome: PyResult<()> = (|| {
        stage(py, &staging_root, files, manifest_json, dag_json)?;
        publish_staged_target(py, &staging_root, &backup_root, target_dir)?;
        for path in &legacy {
            remove_tree(path)?;
        }
        Ok(())
    })();
    let pending: Option<&PyErr> = outcome.as_ref().err();
    for root in [&staging_root, &backup_root] {
        if let Err(failure) = remove_tree(root) {
            return Err(match pending {
                Some(pending) => while_handling(py, failure, pending),
                None => failure,
            });
        }
    }
    outcome
}

/// Compile one project and atomically publish its deterministic static target.
#[pyfunction]
#[pyo3(signature = (*, pipelines_root, loaded_project, adapter_profile, target_dir=None))]
fn run_compile<'py>(
    py: Python<'py>,
    pipelines_root: &Value<'py>,
    loaded_project: &Value<'py>,
    adapter_profile: &Value<'py>,
    target_dir: Option<Value<'py>>,
) -> PyResult<i64> {
    let analysis: Value<'py> = match call(
        py,
        NATIVE,
        "analyze_project",
        &[
            ("pipelines_root", pipelines_root.clone()),
            ("loaded_project", loaded_project.clone()),
            ("adapter_profile", adapter_profile.clone()),
        ],
    ) {
        Ok(analysis) => analysis,
        Err(error)
            if error.is_instance(
                py,
                &class(py, CONTRACT_EXCEPTIONS, "TransformSqlContractError")?,
            ) =>
        {
            let report = || -> PyResult<()> {
                let rendered: Value<'py> =
                    class(py, RENDER_ERROR, "render_error")?.call1((error.value(py),))?;
                print(py, &rendered, true)
            };
            report().map_err(|failure| while_handling(py, failure, &error))?;
            return Ok(1);
        }
        Err(error) => return Err(error),
    };
    let target_dir: Value<'py> = or(
        target_dir.unwrap_or_else(|| none(py)),
        &pipelines_root.getattr("parent")?.div("target")?,
    )?;
    let files: Vec<ArtifactFile> = compiled_files(py, &analysis)?;
    let manifest: String = manifest_json(py, &analysis, &files)?;
    let dag: String = call(
        py,
        NATIVE,
        "build_dag_json",
        &[
            ("graph", analysis.getattr("graph")?),
            (
                "macro_registry",
                analysis
                    .getattr("compile_inputs")?
                    .getattr("macro_registry")?,
            ),
        ],
    )?
    .extract()?;
    publish_static_compile_artifacts(py, &files, &manifest, &dag, &target_dir)?;
    let project: Value<'py> = analysis.getattr("compiled_project")?;
    let message: String = format!(
        "Wrote compile artifacts to {}\nPipelines: {}\nModels: {}",
        formatted(&target_dir)?,
        project.getattr("pipelines")?.len()?,
        project.getattr("models")?.len()?
    );
    print(py, &owned(py, &message), false)?;
    Ok(0)
}

// Path contract shared with the SQL-test runtime target, and test seams for the static target.

/// The target-relative static path of one compiled audit.
#[pyfunction]
#[pyo3(signature = (*, audit, project_dir))]
fn static_audit_path(
    py: Python<'_>,
    audit: &Value<'_>,
    project_dir: &Value<'_>,
) -> PyResult<String> {
    audit_path(py, audit, project_dir)
}

/// The target-relative static path of one source resource.
#[pyfunction]
#[pyo3(signature = (*, source_name, resource_name))]
fn static_source_resource_path(
    py: Python<'_>,
    source_name: &Value<'_>,
    resource_name: &Value<'_>,
) -> PyResult<String> {
    source_resource_path(py, source_name, resource_name)
}

/// The target-relative static path of one SQL test.
#[pyfunction]
#[pyo3(signature = (*, test_case))]
fn static_test_path(py: Python<'_>, test_case: &Value<'_>) -> PyResult<String> {
    test_path(py, test_case)
}

/// The target-relative runtime artifact path for one SQL test, under `run/`.
#[pyfunction]
#[pyo3(signature = (*, test_case))]
fn runtime_test_path<'py>(py: Python<'py>, test_case: &Value<'py>) -> PyResult<Value<'py>> {
    let path_class: Value<'py> = py.import("pathlib")?.getattr("Path")?;
    let parts: Vec<Value<'py>> = items(
        &path_class
            .call1((test_path(py, test_case)?,))?
            .getattr("parts")?,
    )?;
    let mut arguments: Vec<Value<'py>> = vec![owned(py, "run")];
    arguments.extend(parts.into_iter().skip(1));
    path_class.call1(pyo3::types::PyTuple::new(py, arguments)?)
}

/// Stage `(path, contents)` compiled files, the manifest and the DAG under `staging_root`.
#[pyfunction]
#[pyo3(signature = (*, staging_root, compiled_files, manifest_json, dag_json))]
fn stage_static_compile_artifacts(
    py: Python<'_>,
    staging_root: &Value<'_>,
    compiled_files: Vec<ArtifactFile>,
    manifest_json: &str,
    dag_json: &str,
) -> PyResult<()> {
    stage(py, staging_root, &compiled_files, manifest_json, dag_json)
}

/// Reject static artifact paths that collide.
#[pyfunction]
fn validate_unique_artifact_paths(py: Python<'_>, paths: Vec<String>) -> PyResult<()> {
    validate_unique_paths(py, paths.iter().map(String::as_str))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_compile, module)?)?;
    module.add_function(wrap_pyfunction!(static_audit_path, module)?)?;
    module.add_function(wrap_pyfunction!(static_source_resource_path, module)?)?;
    module.add_function(wrap_pyfunction!(static_test_path, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_test_path, module)?)?;
    module.add_function(wrap_pyfunction!(stage_static_compile_artifacts, module)?)?;
    module.add_function(wrap_pyfunction!(validate_unique_artifact_paths, module)?)
}
