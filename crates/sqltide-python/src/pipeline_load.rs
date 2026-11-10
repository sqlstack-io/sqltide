//! Python bindings that load authored pipeline directories: `pipeline.toml` settings, SQL model
//! files, driving-source inference, and pipeline naming validation.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyString, PyTuple};

use crate::config::{parse_pipeline_mode, parse_pipeline_settings};
use crate::discovery::infer_pipeline_sources;
use crate::model_sql::{call, chained, discovery_error, load_transform_from_sql_file, object};
use crate::project_discovery::{
    discover_macro_files, discover_source_registry, source_registry_by_name,
    validate_replay_policies_for_mode,
};
use crate::toml::load_toml;

const NATIVE: &str = "sqltide._native";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const MACRO_MODELS: &str = "sqltide.compiler.macros.models";
const PIPELINE_CONFIG_FILE_NAME: &str = "pipeline.toml";
const PROJECT_CONFIG_FILE_NAME: &str = "sqltide_project.toml";
const SQL_SUFFIX: &str = ".sql";
const VIRTUAL_MODE: &str = "virtual";
const DIRECT_MODE: &str = "direct";
const MACRO_CONTEXT_PARAMETER_NAME: &str = "ctx";
const NAMING_CONTEXT_ANNOTATION_NAME: &str = "PipelineNamingContext";

/// One pipeline directory loaded before its driving source is inferred.
struct Draft<'py> {
    pipeline_dir: Bound<'py, PyAny>,
    transforms: Vec<Bound<'py, PyAny>>,
    mode: Bound<'py, PyAny>,
    settings: Bound<'py, PyTuple>,
}

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

/// The relative path components of every entry named `*.sql` under `root`, in Python's sorted
/// `Path.rglob("*.sql")` order: entries of any type match, and symlinked directories below the
/// root are not followed.
fn sql_paths(root: &Path) -> Vec<Vec<String>> {
    let mut found: Vec<Vec<String>> = Vec::new();
    let mut pending: Vec<(PathBuf, Vec<String>)> = vec![(root.to_path_buf(), Vec::new())];
    while let Some((directory, parts)) = pending.pop() {
        let Ok(entries) = fs::read_dir(&directory) else {
            continue;
        };
        for entry in entries.flatten() {
            let name: String = entry.file_name().to_string_lossy().into_owned();
            let mut child: Vec<String> = parts.clone();
            child.push(name.clone());
            if name.ends_with(SQL_SUFFIX) {
                found.push(child.clone());
            }
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                pending.push((entry.path(), child));
            }
        }
    }
    found.sort();
    found
}

/// `pipeline.toml`'s values, or `{}` without one.
fn pipeline_config<'py>(
    py: Python<'py>,
    config_file: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if config_file.is_none() {
        return Ok(PyDict::new(py).into_any());
    }
    let failure: PyErr = match load_toml(py, &text(&config_file.getattr("contents")?)?) {
        Ok(values) => return Ok(values),
        Err(failure) => failure,
    };
    if !failure.is_instance_of::<PyValueError>(py) {
        return Err(failure);
    }
    let error: Bound<'py, PyAny> = failure.value(py).clone().into_any();
    let raised: PyErr = discovery_error(
        py,
        format!(
            "Pipeline config '{}' contains invalid TOML: {}",
            text(&config_file.getattr("file_path")?)?,
            text(&error)?
        ),
    );
    chained(raised.value(py).as_any(), &error, Some(&error))?;
    Err(raised)
}

/// The SQL models of one pipeline directory in path order.
fn pipeline_transforms<'py>(
    py: Python<'py>,
    pipeline_root: &Bound<'py, PyAny>,
    model_contents_by_path: Option<&Bound<'py, PyAny>>,
    macro_registry: Option<&Bound<'py, PyAny>>,
    macro_context: Option<&Bound<'py, PyAny>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let root: PathBuf = pipeline_root.extract()?;
    let paths: Vec<Vec<String>> = sql_paths(&root);
    if paths.is_empty() {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline root '{}' must contain at least one SQL model file",
                text(pipeline_root)?
            ),
        ));
    }
    let mut transforms: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut paths_by_stem: HashMap<String, Bound<'py, PyAny>> = HashMap::new();
    for parts in paths {
        let file_path: Bound<'py, PyAny> =
            pipeline_root.call_method1("joinpath", PyTuple::new(py, parts)?)?;
        let stem: String = text(&file_path.getattr("stem")?)?;
        if let Some(existing) = paths_by_stem.get(&stem) {
            return Err(discovery_error(
                py,
                format!(
                    "Pipeline root '{}' defines duplicate model filename stem '{stem}' in both \
                     '{}' and '{}'",
                    text(pipeline_root)?,
                    text(existing)?,
                    text(&file_path)?
                ),
            ));
        }
        paths_by_stem.insert(stem, file_path.clone());
        let contents: Option<String> = match model_contents_by_path {
            None => None,
            Some(contents) => Some(contents.get_item(&file_path)?.extract()?),
        };
        transforms.push(load_transform_from_sql_file(
            py,
            &file_path,
            contents,
            macro_registry.cloned(),
            macro_context.cloned(),
        )?);
    }
    Ok(transforms)
}

fn draft<'py>(
    py: Python<'py>,
    pipeline_directory: &Bound<'py, PyAny>,
    model_contents_by_path: Option<&Bound<'py, PyAny>>,
    macro_registry: Option<&Bound<'py, PyAny>>,
    macro_context: Option<&Bound<'py, PyAny>>,
    default_mode: &str,
) -> PyResult<Draft<'py>> {
    let pipeline_dir: Bound<'py, PyAny> = pipeline_directory.getattr("pipeline_dir")?;
    let config_file: Bound<'py, PyAny> = pipeline_directory.getattr("config_file")?;
    let values: Bound<'py, PyAny> = pipeline_config(py, &config_file)?;
    let config_path: Bound<'py, PyAny> = if config_file.is_none() {
        pipeline_dir.call_method1("joinpath", (PIPELINE_CONFIG_FILE_NAME,))?
    } else {
        config_file.getattr("file_path")?
    };
    let config_path: String = text(&config_path)?;
    let mode_name: String = parse_pipeline_mode(py, &values, &config_path, default_mode)?;
    let mode: Bound<'py, PyAny> = py
        .import(DISCOVERY_TYPES)?
        .getattr("PipelineMode")?
        .call1((mode_name.as_str(),))?;
    let is_virtual: bool = mode_name == VIRTUAL_MODE;
    let macro_context: Option<Bound<'py, PyAny>> = match macro_context {
        Some(context) if !context.getattr("virtual_environments")?.eq(is_virtual)? => {
            let keywords: Bound<'py, PyDict> = PyDict::new(py);
            keywords.set_item("virtual_environments", is_virtual)?;
            Some(
                py.import("dataclasses")?
                    .getattr("replace")?
                    .call((context,), Some(&keywords))?,
            )
        }
        context => context.cloned(),
    };
    let transforms: Vec<Bound<'py, PyAny>> = pipeline_transforms(
        py,
        &pipeline_dir,
        model_contents_by_path,
        macro_registry,
        macro_context.as_ref(),
    )?;
    let settings: Bound<'py, PyTuple> = parse_pipeline_settings(
        py,
        &values,
        &config_path,
        &text(&pipeline_dir.getattr("name")?)?,
        &mode_name,
    )?;
    Ok(Draft {
        pipeline_dir,
        transforms,
        mode,
        settings,
    })
}

/// Each table model's driving input by name, rejecting a node name defined twice.
fn transforms_by_name(py: Python<'_>, drafts: &[Draft<'_>]) -> PyResult<BTreeMap<String, String>> {
    let transform_step: Bound<'_, PyAny> = py.import(DISCOVERY_MODELS)?.getattr("TransformStep")?;
    let mut inputs: BTreeMap<String, String> = BTreeMap::new();
    let mut paths: HashMap<String, String> = HashMap::new();
    for draft in drafts {
        for model in &draft.transforms {
            let name: String = model.getattr("name")?.extract()?;
            let source_file_path: Bound<'_, PyAny> = model.getattr("source_file_path")?;
            if let Some(existing) = paths.get(&name) {
                return Err(discovery_error(
                    py,
                    format!(
                        "Logical node name '{name}' is defined in both '{existing}' and '{}'",
                        text(&source_file_path)?
                    ),
                ));
            }
            if model.is_instance(&transform_step)? {
                inputs.insert(name.clone(), model.getattr("source")?.extract()?);
            }
            let path: Bound<'_, PyAny> = if source_file_path.is_truthy()? {
                source_file_path
            } else {
                draft.pipeline_dir.clone()
            };
            paths.insert(name, text(&path)?);
        }
    }
    Ok(inputs)
}

/// `error` raised `from cause` while handling it.
fn discovery_error_from(
    py: Python<'_>,
    message: String,
    cause: &Bound<'_, PyAny>,
) -> PyResult<PyErr> {
    let raised: PyErr = discovery_error(py, message);
    chained(raised.value(py).as_any(), cause, Some(cause))?;
    Ok(raised)
}

/// Require the configured pipeline prefix and, when configured, the naming macro's name.
fn validate_pipeline_name(
    py: Python<'_>,
    draft: &Draft<'_>,
    source: Option<&Bound<'_, PyAny>>,
    project_naming: &Bound<'_, PyAny>,
    macro_registry: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let name: String = text(&draft.pipeline_dir.getattr("name")?)?;
    let directory: String = text(&draft.pipeline_dir)?;
    let prefix: String = project_naming.getattr("pipeline_prefix")?.extract()?;
    if !name.starts_with(&prefix) {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline directory '{directory}' must start with configured \
                 naming.pipeline_prefix '{prefix}'"
            ),
        ));
    }
    let macro_name: Option<String> = project_naming.getattr("pipeline_naming_macro")?.extract()?;
    let Some(macro_name) = macro_name else {
        return Ok(());
    };
    let macros: Option<Bound<'_, PyAny>> = match macro_registry {
        None => None,
        Some(registry) => Some(registry.getattr("macros")?),
    };
    let loaded: Option<Bound<'_, PyAny>> = match &macros {
        None => None,
        Some(macros) => Some(macros.call_method1("get", (macro_name.as_str(),))?)
            .filter(|loaded| !loaded.is_none()),
    };
    let Some(loaded) = loaded else {
        let available: String = match &macros {
            None => "none".to_owned(),
            Some(macros) => {
                let mut names: Vec<String> = macros
                    .try_iter()?
                    .map(|name| name?.extract::<String>())
                    .collect::<PyResult<_>>()?;
                names.sort();
                if names.is_empty() {
                    "none".to_owned()
                } else {
                    names.join(", ")
                }
            }
        };
        return Err(discovery_error(
            py,
            format!(
                "Pipeline naming macro '{macro_name}' is not defined. Available macros: {available}"
            ),
        ));
    };
    let macro_path: String = text(&loaded.getattr("file_path")?)?;
    let function: Bound<'_, PyAny> = loaded.getattr("function")?;
    let inspect: Bound<'_, PyModule> = py.import("inspect")?;
    let parameters: Vec<Bound<'_, PyAny>> = inspect
        .getattr("signature")?
        .call1((&function,))?
        .getattr("parameters")?
        .call_method0("values")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let naming_context: Bound<'_, PyAny> =
        py.import(MACRO_MODELS)?.getattr("PipelineNamingContext")?;
    let valid: bool = parameters.len() == 1 && {
        let parameter: &Bound<'_, PyAny> = &parameters[0];
        let kind: Bound<'_, PyAny> = inspect.getattr("Parameter")?;
        let kinds: Bound<'_, PySet> = PySet::new(
            py,
            [
                kind.getattr("POSITIONAL_ONLY")?,
                kind.getattr("POSITIONAL_OR_KEYWORD")?,
            ],
        )?;
        parameter
            .getattr("name")?
            .eq(MACRO_CONTEXT_PARAMETER_NAME)?
            && kinds.contains(parameter.getattr("kind")?)?
            && PySet::new(
                py,
                [
                    naming_context.clone(),
                    object(py, NAMING_CONTEXT_ANNOTATION_NAME)?,
                ],
            )?
            .contains(parameter.getattr("annotation")?)?
    };
    if !valid {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline naming macro '{macro_name}' in '{macro_path}' must have the signature \
                 def <name>(ctx: PipelineNamingContext) -> str"
            ),
        ));
    }
    let mut model_names: Vec<String> = draft
        .transforms
        .iter()
        .map(|transform| transform.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    model_names.sort();
    let context: Bound<'_, PyAny> = call(
        &naming_context,
        &[
            ("name", object(py, name.as_str())?),
            (
                "source_name",
                match source {
                    None => py.None().into_bound(py),
                    Some(source) => source.getattr("name")?,
                },
            ),
            ("model_names", PyTuple::new(py, model_names)?.into_any()),
            (
                "relative_directory",
                object(py, format!("pipelines/{name}"))?,
            ),
            ("mode", draft.mode.clone()),
        ],
    )?;
    let expected: Bound<'_, PyAny> = match function.call1((context,)) {
        Ok(expected) => expected,
        Err(failure) if failure.is_instance_of::<PyTypeError>(py) => {
            let error: Bound<'_, PyAny> = failure.value(py).clone().into_any();
            return Err(discovery_error_from(
                py,
                format!(
                    "Pipeline naming macro '{macro_name}' in '{macro_path}' must accept exactly \
                     one PipelineNamingContext: {}",
                    text(&error)?
                ),
                &error,
            )?);
        }
        Err(failure) if failure.is_instance_of::<PyException>(py) => {
            let error: Bound<'_, PyAny> = failure.value(py).clone().into_any();
            return Err(discovery_error_from(
                py,
                format!(
                    "Pipeline naming macro '{macro_name}' in '{macro_path}' failed for pipeline \
                     '{name}': {}",
                    text(&error)?
                ),
                &error,
            )?);
        }
        Err(failure) => return Err(failure),
    };
    if !expected.is_instance_of::<PyString>() || !expected.is_truthy()? {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline naming macro '{macro_name}' in '{macro_path}' must return a non-empty \
                 string"
            ),
        ));
    }
    if expected.ne(name.as_str())? {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline directory '{directory}' does not satisfy naming macro '{macro_name}'; \
                 expected 'pipelines/{}'",
                text(&expected)?
            ),
        ));
    }
    Ok(())
}

/// Load pipeline directories and infer each source from driving-input chains.
#[pyfunction]
#[pyo3(signature = (
    *,
    pipeline_directories,
    sources_by_name,
    model_contents_by_path=None,
    macro_registry=None,
    macro_context=None,
    project_naming=None,
    default_mode=None,
))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn load_pipeline_directories<'py>(
    py: Python<'py>,
    pipeline_directories: Vec<Bound<'py, PyAny>>,
    sources_by_name: Bound<'py, PyAny>,
    model_contents_by_path: Option<Bound<'py, PyAny>>,
    macro_registry: Option<Bound<'py, PyAny>>,
    macro_context: Option<Bound<'py, PyAny>>,
    project_naming: Option<Bound<'py, PyAny>>,
    default_mode: Option<String>,
) -> PyResult<Bound<'py, PyTuple>> {
    let default_mode: String = default_mode.unwrap_or_else(|| DIRECT_MODE.to_owned());
    let drafts: Vec<Draft<'py>> = pipeline_directories
        .iter()
        .map(|directory| {
            draft(
                py,
                directory,
                model_contents_by_path.as_ref(),
                macro_registry.as_ref(),
                macro_context.as_ref(),
                &default_mode,
            )
        })
        .collect::<PyResult<_>>()?;
    let inputs: HashMap<String, String> = transforms_by_name(py, &drafts)?.into_iter().collect();
    let transform_step: Bound<'py, PyAny> =
        py.import(DISCOVERY_MODELS)?.getattr("TransformStep")?;
    let pipelines: Vec<(String, Vec<(String, String)>)> = drafts
        .iter()
        .map(|draft| {
            let mut transforms: Vec<(String, String)> = Vec::new();
            for model in &draft.transforms {
                if model.is_instance(&transform_step)? {
                    transforms.push((
                        model.getattr("name")?.extract()?,
                        model.getattr("source")?.extract()?,
                    ));
                }
            }
            Ok((text(&draft.pipeline_dir)?, transforms))
        })
        .collect::<PyResult<_>>()?;
    let sources: HashSet<String> = sources_by_name
        .try_iter()?
        .map(|name| name?.extract::<String>())
        .collect::<PyResult<_>>()?;
    let (source_names, source_error) = infer_pipeline_sources(pipelines, inputs, sources);
    let pipeline_class: Bound<'py, PyAny> = py.import(DISCOVERY_MODELS)?.getattr("Pipeline")?;
    let mut loaded: Vec<Bound<'py, PyAny>> = Vec::new();
    for (index, draft) in drafts.iter().enumerate() {
        if index == source_names.len()
            && let Some(message) = &source_error
        {
            return Err(discovery_error(py, message.clone()));
        }
        let source: Option<Bound<'py, PyAny>> = match &source_names[index] {
            None => None,
            Some(name) => Some(sources_by_name.get_item(name)?),
        };
        if let Some(naming) = &project_naming {
            validate_pipeline_name(py, draft, source.as_ref(), naming, macro_registry.as_ref())?;
        }
        let settings: &Bound<'py, PyTuple> = &draft.settings;
        loaded.push(call(
            &pipeline_class,
            &[
                ("name", draft.pipeline_dir.getattr("name")?),
                ("source", source.unwrap_or_else(|| py.None().into_bound(py))),
                (
                    "transforms",
                    PyTuple::new(py, &draft.transforms)?.into_any(),
                ),
                ("mode", draft.mode.clone()),
                ("replay_on_change", settings.get_item(0)?),
                ("bounded_replay_fallback", settings.get_item(1)?),
                ("naming", settings.get_item(2)?),
                ("protection", settings.get_item(3)?),
                ("audit_defaults", settings.get_item(4)?),
                ("execution_settings", settings.get_item(5)?),
                ("model_retention", settings.get_item(6)?),
                ("model_on_row_error", settings.get_item(7)?),
            ],
        )?);
    }
    PyTuple::new(py, loaded)
}

/// The `Project` an effective project configuration describes.
pub(crate) fn project_from_effective<'py>(
    py: Python<'py>,
    effective: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let defaults: Bound<'py, PyAny> = effective.getattr("defaults")?;
    let models: Bound<'py, PyAny> = defaults.getattr("models")?;
    call(
        &py.import(DISCOVERY_MODELS)?.getattr("Project")?,
        &[
            ("replay_on_change", defaults.getattr("replay_on_change")?),
            ("model_ttl", defaults.getattr("model_ttl")?),
            ("model_retention", models.getattr("retention")?),
            ("model_on_row_error", models.getattr("on_row_error")?),
            ("default_database", effective.getattr("database")?),
            ("adapter", effective.getattr("adapter")?),
            (
                "bounded_replay_fallback",
                defaults.getattr("bounded_replay_fallback")?,
            ),
            ("naming", effective.getattr("naming")?),
            ("audit_defaults", defaults.getattr("audits")?),
            ("audit_scheduler", effective.getattr("audit_scheduler")?),
            ("dependencies", effective.getattr("dependencies")?),
        ],
    )
}

/// Load one pipeline directory without the project compiler's macro expansion phase.
#[pyfunction]
fn load_pipeline_directory<'py>(
    py: Python<'py>,
    pipeline_dir: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let native: Bound<'py, PyModule> = py.import(NATIVE)?;
    let project_dir: Bound<'py, PyAny> = native
        .getattr("find_project_configuration_dir")?
        .call1((&pipeline_dir,))?;
    if project_dir.is_none() {
        return Err(discovery_error(
            py,
            format!(
                "Pipeline directory '{}' is not inside a sqltide_project.toml project",
                text(&pipeline_dir)?
            ),
        ));
    }
    let loaded_configuration: Bound<'py, PyAny> = call(
        &native.getattr("load_project_configuration")?,
        &[("project_dir", project_dir.clone())],
    )?;
    let effective: Bound<'py, PyAny> = call(
        &native.getattr("resolve_effective_project_configuration")?,
        &[
            ("loaded", loaded_configuration),
            ("selected_target", py.None().into_bound(py)),
            ("cli_variables", PyDict::new(py).into_any()),
            ("environment", PyDict::new(py).into_any()),
        ],
    )?;
    let defaults: Bound<'py, PyAny> = effective.getattr("defaults")?;
    let kafka: Bound<'py, PyAny> = defaults.getattr("sources")?.getattr("kafka")?;
    let naming: Bound<'py, PyAny> = effective.getattr("naming")?;
    let macro_registry: Bound<'py, PyAny> = if kafka.getattr("naming_macro")?.is_none()
        && naming.getattr("pipeline_naming_macro")?.is_none()
    {
        py.import(MACRO_MODELS)?.getattr("MacroRegistry")?.call0()?
    } else {
        let macro_files: Bound<'py, PyAny> =
            discover_macro_files(py, project_dir.clone())?.into_any();
        call(
            &wrap_pyfunction!(crate::macros::load_macro_registry, py)?.into_any(),
            &[("macro_files", macro_files)],
        )?
    };
    let source_files: Bound<'py, PyTuple> = discover_source_registry(
        py,
        project_dir.clone(),
        py.import("builtins")?
            .getattr("dict")?
            .call1((effective.getattr("variables")?,))?,
        PyDict::new(py).into_any(),
        defaults.getattr("managed_source_ttl")?.extract()?,
        Some(kafka.getattr("retention")?),
        defaults.getattr("kafka_broker_list")?.extract()?,
        Some(defaults.getattr("freshness")?),
        kafka.getattr("naming_macro")?.extract()?,
        Some(macro_registry.clone()),
    )?;
    let sources_by_name: Bound<'py, PyDict> = source_registry_by_name(py, source_files.into_any())?;
    let config_path: Bound<'py, PyAny> =
        pipeline_dir.call_method1("joinpath", (PIPELINE_CONFIG_FILE_NAME,))?;
    let config_file: Bound<'py, PyAny> = if config_path.call_method0("is_file")?.is_truthy()? {
        let keywords: Bound<'py, PyDict> = PyDict::new(py);
        keywords.set_item("encoding", "utf-8")?;
        call(
            &py.import(DISCOVERY_MODELS)?
                .getattr("DiscoveredProjectFile")?,
            &[
                ("file_path", config_path.clone()),
                (
                    "relative_path",
                    config_path.call_method1("relative_to", (&project_dir,))?,
                ),
                (
                    "contents",
                    config_path.call_method("read_text", (), Some(&keywords))?,
                ),
            ],
        )?
    } else {
        py.None().into_bound(py)
    };
    let directory: Bound<'py, PyAny> = call(
        &py.import(DISCOVERY_MODELS)?
            .getattr("DiscoveredPipelineDirectory")?,
        &[
            ("pipeline_dir", pipeline_dir.clone()),
            ("config_file", config_file),
        ],
    )?;
    let pipeline_mode: Bound<'py, PyAny> = py.import(DISCOVERY_TYPES)?.getattr("PipelineMode")?;
    let default_mode: String = text(&pipeline_mode.call1((defaults.getattr("pipeline_mode")?,))?)?;
    let pipeline: Bound<'py, PyAny> = load_pipeline_directories(
        py,
        vec![directory],
        sources_by_name.into_any(),
        None,
        Some(macro_registry),
        None,
        Some(naming.clone()),
        Some(default_mode),
    )?
    .get_item(0)?;
    let models: Bound<'py, PyModule> = py.import(DISCOVERY_MODELS)?;
    let project: Bound<'py, PyAny> = project_from_effective(py, &effective)?;
    let loaded_pipeline: Bound<'py, PyAny> = call(
        &models.getattr("LoadedPipeline")?,
        &[
            ("pipeline", pipeline),
            ("file_path", pipeline_dir),
            ("project", project.clone()),
        ],
    )?;
    validate_replay_policies_for_mode(
        py,
        pipeline_mode.call1((defaults.getattr("pipeline_mode")?,))?,
        project,
        project_dir.call_method1("joinpath", (PROJECT_CONFIG_FILE_NAME,))?,
        PyTuple::new(py, [&loaded_pipeline])?.into_any(),
    )?;
    Ok(loaded_pipeline)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(load_pipeline_directories, module)?)?;
    module.add_function(wrap_pyfunction!(load_pipeline_directory, module)?)
}
