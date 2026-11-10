//! Python bindings for project discovery: the project configuration, the standalone source
//! registry, retained project files and pipeline directories, and the aggregate input checks.

use pyo3::exceptions::{PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use crate::config::parse_source_registry;
use crate::discovery::{
    PipelineFactsArgument, ProjectPolicyArgument, pipeline_facts, project_policy_facts,
};
use crate::model_sql::{call, chained, discovery_error};
use crate::pipeline_load::{load_pipeline_directories, project_from_effective};
use sqltide_discovery::project_validation::{
    ProjectInputFacts, ProjectReferenceFacts, validate_project_inputs, validate_replay_policies,
};

const NATIVE: &str = "sqltide._native";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const DISCOVERY_EXCEPTIONS: &str = "sqltide.compiler.discovery.exceptions";
const MACRO_CONSTANTS: &str = "sqltide.compiler.macros.constants";
const ACCESS_CONSTANTS: &str = "sqltide.compiler.access.constants";
const LOAD_MACRO_REGISTRY: &str = "sqltide.compiler.macros.main._load_macro_registry";
const SOURCES_DIRECTORY_NAME: &str = "sources";
const MACROS_DIRECTORY_NAME: &str = "macros";
const TESTS_DIRECTORY_NAME: &str = "tests";
const AUDITS_DIRECTORY_NAME: &str = "audits";
const PIPELINE_CONFIG_FILE_NAME: &str = "pipeline.toml";
const PYTHON_PACKAGE_INITIALIZER_FILE_NAME: &str = "__init__.py";
const SOURCE_FILE_PATTERN: &str = "*.yml";
const SQL_FILE_PATTERN: &str = "*.sql";
const PYTHON_FILE_PATTERN: &str = "*.py";
const KAFKA_NAMING_MACRO_TOPIC_PARAMETER: &str = "topic";
const DIRECT_MODE: &str = "direct";
const VIRTUAL_MODE: &str = "virtual";

fn text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

fn none(py: Python<'_>) -> Bound<'_, PyAny> {
    py.None().into_bound(py)
}

/// `value`, or `None` when it is Python's `None`.
fn present<'py>(value: Bound<'py, PyAny>) -> Option<Bound<'py, PyAny>> {
    (!value.is_none()).then_some(value)
}

fn joined<'py>(path: &Bound<'py, PyAny>, part: &str) -> PyResult<Bound<'py, PyAny>> {
    path.call_method1("joinpath", (part,))
}

fn sorted<'py>(py: Python<'py>, values: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    py.import("builtins")?
        .getattr("sorted")?
        .call1((values,))?
        .try_iter()?
        .collect()
}

/// `class(str(failure))` raised `from None` while handling `failure` when it is a `ValueError`;
/// any other failure propagates unchanged.
fn converted(py: Python<'_>, failure: PyErr, module: &str, class: &str) -> PyErr {
    if !failure.is_instance_of::<PyValueError>(py) {
        return failure;
    }
    let raised = || -> PyResult<PyErr> {
        let error: Bound<'_, PyAny> = failure.value(py).clone().into_any();
        let raised: Bound<'_, PyAny> =
            py.import(module)?.getattr(class)?.call1((text(&error)?,))?;
        chained(&raised, &error, None)?;
        Ok(PyErr::from_value(raised))
    };
    raised().unwrap_or_else(|error| error)
}

fn pipeline_discovery_failure(py: Python<'_>, message: String) -> PyErr {
    converted(
        py,
        PyValueError::new_err(message),
        DISCOVERY_EXCEPTIONS,
        "PipelineDiscoveryError",
    )
}

/// `DiscoveredProjectFile(file_path, relative_path, contents)` for one file read with `read`.
fn project_file<'py>(
    py: Python<'py>,
    file_path: &Bound<'py, PyAny>,
    project_dir: &Bound<'py, PyAny>,
    contents: impl FnOnce() -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let relative_path: Bound<'py, PyAny> = file_path.call_method1("relative_to", (project_dir,))?;
    let contents: Bound<'py, PyAny> = contents()?;
    call(
        &py.import(DISCOVERY_MODELS)?
            .getattr("DiscoveredProjectFile")?,
        &[
            ("file_path", file_path.clone()),
            ("relative_path", relative_path),
            ("contents", contents),
        ],
    )
}

fn read_text<'py>(path: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(path.py());
    keywords.set_item("encoding", "utf-8")?;
    path.call_method("read_text", (), Some(&keywords))
}

/// Read sorted project paths into immutable source snapshots, decoding bytes as UTF-8 without
/// newline translation.
#[pyfunction]
#[pyo3(signature = (*, file_paths, project_dir))]
pub(crate) fn read_discovered_files<'py>(
    py: Python<'py>,
    file_paths: Bound<'py, PyAny>,
    project_dir: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let files: Vec<Bound<'py, PyAny>> = sorted(py, &file_paths)?
        .iter()
        .map(|file_path| {
            project_file(py, file_path, &project_dir, || {
                file_path
                    .call_method0("read_bytes")?
                    .call_method1("decode", ("utf-8",))
            })
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, files)
}

/// Every `*.<suffix>` path under `root`, recursively, as `Path.rglob` yields them.
fn rglob<'py>(root: &Bound<'py, PyAny>, pattern: &str) -> PyResult<Bound<'py, PyAny>> {
    let paths: Vec<Bound<'py, PyAny>> = root
        .call_method1("rglob", (pattern,))?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    Ok(PyTuple::new(root.py(), paths)?.into_any())
}

fn read_tree<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    project_dir: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    read_discovered_files(py, rglob(root, SQL_FILE_PATTERN)?, project_dir.clone())
}

/// Public project macro modules under `macros/`, read once in stable path order: no package
/// initializers and no path part starting with `_`.
#[pyfunction]
#[pyo3(signature = (*, project_dir))]
pub(crate) fn discover_macro_files<'py>(
    py: Python<'py>,
    project_dir: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let root: Bound<'py, PyAny> = joined(&project_dir, MACROS_DIRECTORY_NAME)?;
    let mut files: Vec<Bound<'py, PyAny>> = Vec::new();
    for path in sorted(py, &root.call_method1("rglob", (PYTHON_FILE_PATTERN,))?)? {
        let relative: Bound<'py, PyAny> = path.call_method1("relative_to", (&project_dir,))?;
        if path
            .getattr("name")?
            .eq(PYTHON_PACKAGE_INITIALIZER_FILE_NAME)?
        {
            continue;
        }
        let mut private: bool = false;
        for part in relative.getattr("parts")?.try_iter()? {
            if text(&part?)?.starts_with('_') {
                private = true;
                break;
            }
        }
        if private {
            continue;
        }
        files.push(project_file(py, &path, &project_dir, || read_text(&path))?);
    }
    PyTuple::new(py, files)
}

/// Discover direct pipeline directories and retain each optional `pipeline.toml`.
#[pyfunction]
#[pyo3(signature = (*, pipelines_root, project_dir))]
pub(crate) fn discover_pipeline_directories<'py>(
    py: Python<'py>,
    pipelines_root: Bound<'py, PyAny>,
    project_dir: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    if !pipelines_root.call_method0("is_dir")?.is_truthy()? {
        return Ok(PyTuple::empty(py));
    }
    let mut pipeline_dirs: Vec<Bound<'py, PyAny>> = Vec::new();
    for path in pipelines_root.call_method0("iterdir")?.try_iter()? {
        let path: Bound<'py, PyAny> = path?;
        if path.call_method0("is_dir")?.is_truthy()? {
            pipeline_dirs.push(path);
        }
    }
    let pipeline_dirs: Vec<Bound<'py, PyAny>> =
        sorted(py, &PyList::new(py, pipeline_dirs)?.into_any())?;
    let mut config_paths: Vec<Bound<'py, PyAny>> = Vec::new();
    for pipeline_dir in &pipeline_dirs {
        let config_path: Bound<'py, PyAny> = joined(pipeline_dir, PIPELINE_CONFIG_FILE_NAME)?;
        if config_path.call_method0("is_file")?.is_truthy()? {
            config_paths.push(config_path);
        }
    }
    let config_files: Bound<'py, PyTuple> =
        read_discovered_files(py, PyTuple::new(py, config_paths)?.into_any(), project_dir)?;
    let config_by_dir: Bound<'py, PyDict> = PyDict::new(py);
    for config_file in config_files.iter() {
        config_by_dir.set_item(
            config_file.getattr("file_path")?.getattr("parent")?,
            &config_file,
        )?;
    }
    let directory_class: Bound<'py, PyAny> = py
        .import(DISCOVERY_MODELS)?
        .getattr("DiscoveredPipelineDirectory")?;
    let directories: Vec<Bound<'py, PyAny>> = pipeline_dirs
        .iter()
        .map(|pipeline_dir| {
            call(
                &directory_class,
                &[
                    ("pipeline_dir", pipeline_dir.clone()),
                    (
                        "config_file",
                        config_by_dir
                            .get_item(pipeline_dir)?
                            .unwrap_or_else(|| none(py)),
                    ),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, directories)
}

/// `LoadedPipeline`s attached to `project` for each directory and its loaded pipeline.
fn loaded_pipelines<'py>(
    py: Python<'py>,
    pipeline_directories: &Bound<'py, PyTuple>,
    pipelines: &Bound<'py, PyTuple>,
    project: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let class: Bound<'py, PyAny> = py.import(DISCOVERY_MODELS)?.getattr("LoadedPipeline")?;
    pipeline_directories
        .iter()
        .zip(pipelines.iter())
        .map(|(directory, pipeline)| {
            call(
                &class,
                &[
                    ("pipeline", pipeline),
                    ("file_path", directory.getattr("pipeline_dir")?),
                    ("project", project.clone()),
                ],
            )
        })
        .collect()
}

/// Parse retained pipeline and model sources without rereading either kind.
#[pyfunction]
#[pyo3(signature = (
    *,
    pipeline_directories,
    model_files,
    macro_registry,
    macro_context,
    sources_by_name,
    project,
    project_naming,
    default_mode,
))]
#[allow(clippy::too_many_arguments)]
fn load_discovered_pipelines<'py>(
    py: Python<'py>,
    pipeline_directories: Bound<'py, PyTuple>,
    model_files: Bound<'py, PyAny>,
    macro_registry: Bound<'py, PyAny>,
    macro_context: Bound<'py, PyAny>,
    sources_by_name: Bound<'py, PyAny>,
    project: Bound<'py, PyAny>,
    project_naming: Bound<'py, PyAny>,
    default_mode: String,
) -> PyResult<Bound<'py, PyTuple>> {
    let contents: Bound<'py, PyDict> = PyDict::new(py);
    for model_file in model_files.try_iter()? {
        let model_file: Bound<'py, PyAny> = model_file?;
        contents.set_item(
            model_file.getattr("file_path")?,
            model_file.getattr("contents")?,
        )?;
    }
    let pipelines: Bound<'py, PyTuple> = load_pipeline_directories(
        py,
        pipeline_directories.iter().collect(),
        sources_by_name,
        Some(contents.into_any()),
        present(macro_registry),
        present(macro_context),
        present(project_naming),
        Some(default_mode),
    )?;
    PyTuple::new(
        py,
        loaded_pipelines(py, &pipeline_directories, &pipelines, &project)?,
    )
}

/// Load all project source kinds once in stable path order.
#[pyfunction]
#[pyo3(signature = (*, pipelines_root, loaded_project))]
fn discover_project_inputs<'py>(
    py: Python<'py>,
    pipelines_root: Bound<'py, PyAny>,
    loaded_project: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let project_dir: Bound<'py, PyAny> = pipelines_root.getattr("parent")?;
    let pipeline_directories: Bound<'py, PyTuple> =
        discover_pipeline_directories(py, pipelines_root.clone(), project_dir.clone())?;
    let model_files: Bound<'py, PyTuple> = read_tree(py, &pipelines_root, &project_dir)?;
    let test_files: Bound<'py, PyTuple> = read_tree(
        py,
        &joined(&project_dir, TESTS_DIRECTORY_NAME)?,
        &project_dir,
    )?;
    let audit_files: Bound<'py, PyTuple> = read_tree(
        py,
        &joined(&project_dir, AUDITS_DIRECTORY_NAME)?,
        &project_dir,
    )?;
    let (macro_files, source_files): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
        if loaded_project.is_none() {
            (
                discover_macro_files(py, project_dir.clone())?.into_any(),
                PyTuple::empty(py).into_any(),
            )
        } else {
            (
                loaded_project.getattr("macro_files")?,
                loaded_project.getattr("source_files")?,
            )
        };
    let access_name: Bound<'py, PyAny> = py
        .import(ACCESS_CONSTANTS)?
        .getattr("ACCESS_POLICY_FILE_NAME")?;
    let access_path: Bound<'py, PyAny> = project_dir.call_method1("joinpath", (access_name,))?;
    let access_paths: Bound<'py, PyTuple> = if access_path.call_method0("is_file")?.is_truthy()? {
        PyTuple::new(py, [access_path])?
    } else {
        PyTuple::empty(py)
    };
    let access_files: Bound<'py, PyTuple> =
        read_discovered_files(py, access_paths.into_any(), project_dir.clone())?;
    let macro_constants: Bound<'py, PyModule> = py.import(MACRO_CONSTANTS)?;
    let constants_root: Bound<'py, PyAny> = project_dir.call_method1(
        "joinpath",
        (macro_constants.getattr("CONSTANTS_DIRECTORY_NAME")?,),
    )?;
    let constant_files: Bound<'py, PyTuple> = read_tree(py, &constants_root, &project_dir)?;
    let enums_root: Bound<'py, PyAny> = project_dir.call_method1(
        "joinpath",
        (macro_constants.getattr("ENUMS_DIRECTORY_NAME")?,),
    )?;
    let enum_files: Bound<'py, PyTuple> = read_tree(py, &enums_root, &project_dir)?;
    call(
        &py.import(DISCOVERY_MODELS)?
            .getattr("DiscoveredProjectInputs")?,
        &[
            ("project_dir", project_dir),
            ("loaded_project", loaded_project),
            ("source_files", source_files),
            ("pipeline_directories", pipeline_directories.into_any()),
            ("model_files", model_files.into_any()),
            ("test_files", test_files.into_any()),
            ("audit_files", audit_files.into_any()),
            ("macro_files", macro_files),
            (
                "access_file",
                if access_files.is_empty() {
                    none(py)
                } else {
                    access_files.get_item(0)?
                },
            ),
            ("constant_files", constant_files.into_any()),
            ("enum_files", enum_files.into_any()),
        ],
    )
}

/// The `PipelineMode` of an effective configuration's default, `direct` without one.
fn default_mode<'py>(
    py: Python<'py>,
    effective: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let modes: Bound<'py, PyAny> = py.import(DISCOVERY_TYPES)?.getattr("PipelineMode")?;
    if effective.is_none() {
        return modes.getattr("DIRECT");
    }
    modes.call1((effective.getattr("defaults")?.getattr("pipeline_mode")?,))
}

/// Load every pipeline under one `pipelines/` root of a project.
#[pyfunction]
fn discover_pipelines<'py>(
    py: Python<'py>,
    root: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let loaded_project: Bound<'py, PyAny> =
        load_project_input_for_path(py, root.clone(), None, None, None)?;
    if loaded_project.is_none() {
        return Ok(PyList::empty(py));
    }
    let sources_by_name: Bound<'py, PyDict> =
        source_registry_by_name(py, loaded_project.getattr("source_files")?)?;
    let pipeline_directories: Bound<'py, PyTuple> =
        discover_pipeline_directories(py, root.clone(), root.getattr("parent")?)?;
    let effective: Bound<'py, PyAny> = loaded_project.getattr("effective_configuration")?;
    let naming: Bound<'py, PyAny> = if effective.is_none() {
        py.import(DISCOVERY_MODELS)?
            .getattr("ProjectNaming")?
            .call0()?
    } else {
        effective.getattr("naming")?
    };
    let pipelines: Bound<'py, PyTuple> = load_pipeline_directories(
        py,
        pipeline_directories.iter().collect(),
        sources_by_name.into_any(),
        None,
        present(loaded_project.getattr("macro_registry")?),
        None,
        Some(naming),
        Some(text(&default_mode(py, &effective)?)?),
    )?;
    PyList::new(
        py,
        loaded_pipelines(
            py,
            &pipeline_directories,
            &pipelines,
            &loaded_project.getattr("project")?,
        )?,
    )
}

/// The Kafka source name the project naming macro derives from `topic`, with the macro's name
/// and identity fingerprint.
fn derive_kafka_source_name(
    py: Python<'_>,
    topic: &str,
    label: &str,
    file_path: &str,
    macro_name: Option<&str>,
    macro_registry: Option<&Bound<'_, PyAny>>,
) -> PyResult<(String, String, String)> {
    let Some(macro_name) = macro_name else {
        return Err(discovery_error(
            py,
            format!(
                "Source file '{file_path}' {label}.name must be a non-empty string or \
                 defaults.sources.kafka.naming_macro must be configured"
            ),
        ));
    };
    let macros: Option<Bound<'_, PyAny>> = match macro_registry {
        None => None,
        Some(registry) => Some(registry.getattr("macros")?),
    };
    let loaded: Option<Bound<'_, PyAny>> = match &macros {
        None => None,
        Some(macros) => present(macros.call_method1("get", (macro_name,))?),
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
                "Source file '{file_path}' {label}.name cannot use unknown Kafka naming macro \
                 '{macro_name}'. Available macros: {available}"
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
    let valid: bool = parameters.len() == 1 && {
        let parameter: &Bound<'_, PyAny> = &parameters[0];
        let kind: Bound<'_, PyAny> = inspect.getattr("Parameter")?;
        parameter
            .getattr("name")?
            .eq(KAFKA_NAMING_MACRO_TOPIC_PARAMETER)?
            && PySet::new(
                py,
                [
                    kind.getattr("POSITIONAL_ONLY")?,
                    kind.getattr("POSITIONAL_OR_KEYWORD")?,
                ],
            )?
            .contains(parameter.getattr("kind")?)?
    };
    if !valid {
        return Err(discovery_error(
            py,
            format!(
                "Kafka source naming macro '{macro_name}' in '{macro_path}' must have the \
                 signature def <name>(topic: str) -> str"
            ),
        ));
    }
    let result: Bound<'_, PyAny> = match function.call1((topic,)) {
        Ok(result) => result,
        Err(failure) if failure.is_instance_of::<PyException>(py) => {
            let error: Bound<'_, PyAny> = failure.value(py).clone().into_any();
            let message: String = if failure.is_instance_of::<PyTypeError>(py) {
                format!(
                    "Kafka source naming macro '{macro_name}' in '{macro_path}' must accept \
                     exactly one topic string: {}",
                    text(&error)?
                )
            } else {
                format!(
                    "Kafka source naming macro '{macro_name}' in '{macro_path}' failed for \
                     topic '{topic}': {}",
                    text(&error)?
                )
            };
            let raised: PyErr = discovery_error(py, message);
            chained(raised.value(py).as_any(), &error, Some(&error))?;
            return Err(raised);
        }
        Err(failure) => return Err(failure),
    };
    if !result.is_instance_of::<PyString>() {
        return Err(discovery_error(
            py,
            format!(
                "Kafka source naming macro '{macro_name}' in '{macro_path}' must return a string"
            ),
        ));
    }
    Ok((
        result.extract()?,
        loaded.getattr("name")?.extract()?,
        loaded.getattr("identity_fingerprint")?.extract()?,
    ))
}

/// `(path, payload, decode error)` of one source file decoded with PyYAML's `safe_load`.
fn decoded_source_file<'py>(
    py: Python<'py>,
    source_file: &Bound<'py, PyAny>,
) -> PyResult<(String, Bound<'py, PyAny>, Option<String>)> {
    let path: String = text(&source_file.getattr("file_path")?)?;
    let yaml: Bound<'py, PyModule> = py.import("yaml")?;
    match yaml
        .getattr("safe_load")?
        .call1((source_file.getattr("contents")?,))
    {
        Ok(payload) => Ok((path, payload, None)),
        Err(failure) if failure.is_instance(py, &yaml.getattr("YAMLError")?) => {
            Ok((path, none(py), Some(text(failure.value(py).as_any())?)))
        }
        Err(failure) => Err(failure),
    }
}

/// Load `sources/*.yml` once in stable order into `DiscoveredSourceFile`s.
#[pyfunction]
#[pyo3(signature = (
    *,
    project_dir,
    variables,
    environment,
    default_managed_source_ttl=None,
    default_managed_source_retention=None,
    default_kafka_broker_list=None,
    default_freshness=None,
    default_kafka_naming_macro=None,
    macro_registry=None,
))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn discover_source_registry<'py>(
    py: Python<'py>,
    project_dir: Bound<'py, PyAny>,
    variables: Bound<'py, PyAny>,
    environment: Bound<'py, PyAny>,
    default_managed_source_ttl: Option<String>,
    default_managed_source_retention: Option<Bound<'py, PyAny>>,
    default_kafka_broker_list: Option<String>,
    default_freshness: Option<Bound<'py, PyAny>>,
    default_kafka_naming_macro: Option<String>,
    macro_registry: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let sources_root: Bound<'py, PyAny> = joined(&project_dir, SOURCES_DIRECTORY_NAME)?;
    if !sources_root.call_method0("is_dir")?.is_truthy()? {
        return Ok(PyTuple::empty(py));
    }
    let source_files: Vec<Bound<'py, PyAny>> = sorted(
        py,
        &sources_root.call_method1("glob", (SOURCE_FILE_PATTERN,))?,
    )?
    .iter()
    .map(|path| project_file(py, path, &project_dir, || read_text(path)))
    .collect::<PyResult<_>>()?;
    let decoded: Vec<(String, Bound<'py, PyAny>, Option<String>)> = source_files
        .iter()
        .map(|source_file| decoded_source_file(py, source_file))
        .collect::<PyResult<_>>()?;
    let macro_registry: Option<Bound<'py, PyAny>> = macro_registry.and_then(present);
    let mut derive = |topic: &str, label: &str, file_path: &str| {
        derive_kafka_source_name(
            py,
            topic,
            label,
            file_path,
            default_kafka_naming_macro.as_deref(),
            macro_registry.as_ref(),
        )
    };
    let parsed: Vec<Bound<'py, PyTuple>> = parse_source_registry(
        py,
        decoded,
        &variables,
        environment,
        default_managed_source_ttl,
        default_managed_source_retention.unwrap_or_else(|| none(py)),
        default_kafka_broker_list,
        default_freshness.unwrap_or_else(|| none(py)),
        &mut derive,
    )?;
    let class: Bound<'py, PyAny> = py
        .import(DISCOVERY_MODELS)?
        .getattr("DiscoveredSourceFile")?;
    let files: Vec<Bound<'py, PyAny>> = source_files
        .into_iter()
        .zip(parsed)
        .map(|(source_file, sources)| {
            call(
                &class,
                &[
                    ("source_file", source_file),
                    ("sources", sources.into_any()),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, files)
}

/// Index one validated source registry by project-wide source name.
#[pyfunction]
pub(crate) fn source_registry_by_name<'py>(
    py: Python<'py>,
    source_files: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let sources_by_name: Bound<'py, PyDict> = PyDict::new(py);
    for source_file in source_files.try_iter()? {
        for source in source_file?.getattr("sources")?.try_iter()? {
            let source: Bound<'py, PyAny> = source?;
            sources_by_name.set_item(source.getattr("name")?, &source)?;
        }
    }
    Ok(sources_by_name)
}

/// Load the nearest project configuration with its retained sources, or `None` outside a
/// project.
#[pyfunction]
#[pyo3(signature = (*, path, selected_target=None, cli_variables=None, environment=None))]
pub(crate) fn load_project_input_for_path<'py>(
    py: Python<'py>,
    path: Bound<'py, PyAny>,
    selected_target: Option<Bound<'py, PyAny>>,
    cli_variables: Option<Bound<'py, PyAny>>,
    environment: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let native: Bound<'py, PyModule> = py.import(NATIVE)?;
    let project_dir: Bound<'py, PyAny> = native
        .getattr("find_project_configuration_dir")?
        .call1((&path,))?;
    if project_dir.is_none() {
        return Ok(project_dir);
    }
    let configuration: Bound<'py, PyAny> = call(
        &native.getattr("load_project_configuration")?,
        &[("project_dir", project_dir.clone())],
    )?;
    let environment: Bound<'py, PyAny> = environment
        .and_then(present)
        .unwrap_or_else(|| PyDict::new(py).into_any());
    let effective: Bound<'py, PyAny> = call(
        &native.getattr("resolve_effective_project_configuration")?,
        &[
            ("loaded", configuration.clone()),
            (
                "selected_target",
                selected_target.unwrap_or_else(|| none(py)),
            ),
            (
                "cli_variables",
                cli_variables
                    .and_then(present)
                    .unwrap_or_else(|| PyDict::new(py).into_any()),
            ),
            ("environment", environment.clone()),
        ],
    )?;
    let macro_files: Bound<'py, PyTuple> = discover_macro_files(py, project_dir.clone())?;
    let defaults: Bound<'py, PyAny> = effective.getattr("defaults")?;
    let kafka: Bound<'py, PyAny> = defaults.getattr("sources")?.getattr("kafka")?;
    let macro_registry: Option<Bound<'py, PyAny>> = if kafka.getattr("naming_macro")?.is_none()
        && effective
            .getattr("naming")?
            .getattr("pipeline_naming_macro")?
            .is_none()
    {
        None
    } else {
        Some(call(
            &py.import(LOAD_MACRO_REGISTRY)?
                .getattr("load_macro_registry")?,
            &[("macro_files", macro_files.clone().into_any())],
        )?)
    };
    let project: Bound<'py, PyAny> = project_from_effective(py, &effective)?;
    let source_files: Bound<'py, PyTuple> = discover_source_registry(
        py,
        project_dir,
        py.import("builtins")?
            .getattr("dict")?
            .call1((effective.getattr("variables")?,))?,
        environment,
        defaults.getattr("managed_source_ttl")?.extract()?,
        Some(kafka.getattr("retention")?),
        defaults.getattr("kafka_broker_list")?.extract()?,
        Some(defaults.getattr("freshness")?),
        kafka.getattr("naming_macro")?.extract()?,
        macro_registry.clone(),
    )?;
    call(
        &py.import(DISCOVERY_MODELS)?.getattr("LoadedProject")?,
        &[
            ("project", project),
            ("source_file", configuration.getattr("project_source")?),
            ("configuration", configuration),
            ("effective_configuration", effective),
            ("source_files", source_files.into_any()),
            ("macro_files", macro_files.into_any()),
            ("macro_registry", macro_registry.unwrap_or_else(|| none(py))),
        ],
    )
}

/// `(name, file path, is virtual, defines replay_on_change, defines bounded_replay_fallback,
/// models)` of each loaded pipeline.
fn pipeline_fact_arguments(
    py: Python<'_>,
    loaded_pipelines: &Bound<'_, PyAny>,
) -> PyResult<Vec<PipelineFactsArgument>> {
    let transform_step: Bound<'_, PyAny> = py.import(DISCOVERY_MODELS)?.getattr("TransformStep")?;
    let mut facts: Vec<PipelineFactsArgument> = Vec::new();
    for loaded in loaded_pipelines.try_iter()? {
        let loaded: Bound<'_, PyAny> = loaded?;
        let pipeline: Bound<'_, PyAny> = loaded.getattr("pipeline")?;
        let mut models = Vec::new();
        for model in pipeline.getattr("transforms")?.try_iter()? {
            let model: Bound<'_, PyAny> = model?;
            let name: String = model.getattr("name")?.extract()?;
            models.push(if model.is_instance(&transform_step)? {
                (
                    name,
                    true,
                    !model.getattr("replay_on_change")?.is_none(),
                    !model.getattr("bounded_replay_fallback")?.is_none(),
                )
            } else {
                (name, false, false, false)
            });
        }
        facts.push((
            pipeline.getattr("name")?.extract()?,
            text(&loaded.getattr("file_path")?)?,
            pipeline.getattr("mode")?.eq(VIRTUAL_MODE)?,
            !pipeline.getattr("replay_on_change")?.is_none(),
            !pipeline.getattr("bounded_replay_fallback")?.is_none(),
            models,
        ));
    }
    Ok(facts)
}

/// Project-level replay policy facts, or `None` without a project.
fn project_policy_argument(
    default_pipeline_mode: &Bound<'_, PyAny>,
    project: &Bound<'_, PyAny>,
    project_file_path: &Bound<'_, PyAny>,
) -> PyResult<Option<ProjectPolicyArgument>> {
    if project.is_none() {
        return Ok(None);
    }
    Ok(Some((
        text(project_file_path)?,
        default_pipeline_mode.eq(DIRECT_MODE)?,
        !project.getattr("replay_on_change")?.is_none(),
        !project.getattr("bounded_replay_fallback")?.is_none(),
    )))
}

/// Reject change-driven replay policy at every authored level of a direct-mode project.
#[pyfunction]
#[pyo3(signature = (*, default_pipeline_mode, project, project_file_path, loaded_pipelines))]
pub(crate) fn validate_replay_policies_for_mode(
    py: Python<'_>,
    default_pipeline_mode: Bound<'_, PyAny>,
    project: Bound<'_, PyAny>,
    project_file_path: Bound<'_, PyAny>,
    loaded_pipelines: Bound<'_, PyAny>,
) -> PyResult<()> {
    let project_policy: Option<ProjectPolicyArgument> =
        project_policy_argument(&default_pipeline_mode, &project, &project_file_path)?;
    let pipelines = pipeline_facts(pipeline_fact_arguments(py, &loaded_pipelines)?);
    validate_replay_policies(project_policy_facts(project_policy).as_ref(), &pipelines)
        .map_err(|message| pipeline_discovery_failure(py, message))
}

/// `(name or file stem, file path)` of each loaded SQL test or audit.
fn named_files(items: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    items
        .try_iter()?
        .map(|item| {
            let item: Bound<'_, PyAny> = item?;
            let file_path: Bound<'_, PyAny> = item.getattr("file_path")?;
            let name: Bound<'_, PyAny> = item.getattr("name")?;
            Ok((
                if name.is_none() {
                    file_path.getattr("stem")?.extract()?
                } else {
                    name.extract()?
                },
                text(&file_path)?,
            ))
        })
        .collect()
}

/// Reject duplicate project identities and invalid effective replay policy once every input is
/// attached.
#[pyfunction]
#[pyo3(signature = (*, source_files, loaded_project, loaded_pipelines, loaded_tests, loaded_audits))]
fn validate_attached_project_inputs(
    py: Python<'_>,
    source_files: Bound<'_, PyAny>,
    loaded_project: Bound<'_, PyAny>,
    loaded_pipelines: Bound<'_, PyAny>,
    loaded_tests: Bound<'_, PyAny>,
    loaded_audits: Bound<'_, PyAny>,
) -> PyResult<()> {
    let mut sources: Vec<(String, String)> = Vec::new();
    for source_file in source_files.try_iter()? {
        let source_file: Bound<'_, PyAny> = source_file?;
        let path: String = text(&source_file.getattr("source_file")?.getattr("file_path")?)?;
        for source in source_file.getattr("sources")?.try_iter()? {
            sources.push((source?.getattr("name")?.extract()?, path.clone()));
        }
    }
    let pipelines = pipeline_facts(pipeline_fact_arguments(py, &loaded_pipelines)?);
    let (project_references, project_policy) = if loaded_project.is_none() {
        let mode: Bound<'_, PyAny> = default_mode(py, &none(py))?;
        (None, project_policy_argument(&mode, &none(py), &none(py))?)
    } else {
        let source_path: Bound<'_, PyAny> = loaded_project
            .getattr("source_file")?
            .getattr("file_path")?;
        let project: Bound<'_, PyAny> = loaded_project.getattr("project")?;
        let mut references: Vec<(String, String)> = Vec::new();
        for reference in project
            .getattr("dependencies")?
            .getattr("allowed_cross_pipeline_references")?
            .try_iter()?
        {
            let reference: Bound<'_, PyAny> = reference?;
            references.push((
                reference.getattr("upstream_pipeline")?.extract()?,
                reference.getattr("downstream_pipeline")?.extract()?,
            ));
        }
        let mode: Bound<'_, PyAny> =
            default_mode(py, &loaded_project.getattr("effective_configuration")?)?;
        (
            Some(ProjectReferenceFacts {
                file_path: text(&source_path)?,
                allowed_cross_pipeline_references: references,
            }),
            project_policy_argument(&mode, &project, &source_path)?,
        )
    };
    validate_project_inputs(&ProjectInputFacts {
        sources,
        pipelines,
        project_references,
        project_policy: project_policy_facts(project_policy),
        tests: named_files(&loaded_tests)?,
        audits: named_files(&loaded_audits)?,
    })
    .map_err(|message| pipeline_discovery_failure(py, message))
}

fn immutable_config_value<'py>(
    py: Python<'py>,
    value: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let mapping: Bound<'py, PyAny> = py.import("collections.abc")?.getattr("Mapping")?;
    if value.is_instance(&mapping)? {
        let frozen: Bound<'py, PyDict> = PyDict::new(py);
        for item in value.call_method0("items")?.try_iter()? {
            let (key, item): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item?.extract()?;
            frozen.set_item(key.str()?, immutable_config_value(py, item)?)?;
        }
        return py
            .import("types")?
            .getattr("MappingProxyType")?
            .call1((frozen,));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        let items: Vec<Bound<'py, PyAny>> = value
            .try_iter()?
            .map(|item| immutable_config_value(py, item?))
            .collect::<PyResult<_>>()?;
        return Ok(PyTuple::new(py, items)?.into_any());
    }
    Ok(value)
}

/// Freeze nested configuration values while preserving authored key order.
#[pyfunction]
fn immutable_config_pairs<'py>(
    py: Python<'py>,
    values: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let pairs: Vec<Bound<'py, PyAny>> = values
        .try_iter()?
        .map(|pair| {
            let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) = pair?.extract()?;
            Ok(PyTuple::new(py, [key, immutable_config_value(py, value)?])?.into_any())
        })
        .collect::<PyResult<_>>()?;
    PyTuple::new(py, pairs)
}

/// Parse a MODEL, AUDIT or TEST header with the shared key-value grammar, raising
/// `ModelHeaderSyntaxError` for invalid syntax.
#[pyfunction]
#[pyo3(signature = (*, header))]
pub(crate) fn parse_sql_header<'py>(py: Python<'py>, header: &str) -> PyResult<Bound<'py, PyAny>> {
    crate::parse_model_header(py, header)
        .map_err(|failure| converted(py, failure, DISCOVERY_EXCEPTIONS, "ModelHeaderSyntaxError"))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(read_discovered_files, module)?)?;
    module.add_function(wrap_pyfunction!(discover_macro_files, module)?)?;
    module.add_function(wrap_pyfunction!(discover_pipeline_directories, module)?)?;
    module.add_function(wrap_pyfunction!(load_discovered_pipelines, module)?)?;
    module.add_function(wrap_pyfunction!(discover_project_inputs, module)?)?;
    module.add_function(wrap_pyfunction!(discover_pipelines, module)?)?;
    module.add_function(wrap_pyfunction!(discover_source_registry, module)?)?;
    module.add_function(wrap_pyfunction!(source_registry_by_name, module)?)?;
    module.add_function(wrap_pyfunction!(load_project_input_for_path, module)?)?;
    module.add_function(wrap_pyfunction!(validate_replay_policies_for_mode, module)?)?;
    module.add_function(wrap_pyfunction!(validate_attached_project_inputs, module)?)?;
    module.add_function(wrap_pyfunction!(immutable_config_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(parse_sql_header, module)?)
}
