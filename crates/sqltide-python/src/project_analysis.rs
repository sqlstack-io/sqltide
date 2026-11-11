//! Native compile orchestration: compile inputs, project analysis phases, SQL Rule evaluation and
//! compiler-verified formatting. Authored sensors, macro modules and adapter callbacks are still
//! called as Python.

use std::collections::HashSet;
use std::time::Instant;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PySlice, PyTuple};

use crate::analysis::sql_canonicalize;
use crate::graph::resolve_named_model_keys;

const NATIVE: &str = "sqltide._native";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const MACRO_MODELS: &str = "sqltide.compiler.macros.models";
const PIPELINE_MODELS: &str = "sqltide.compiler.pipeline.models";
const RULES_MODELS: &str = "sqltide.compiler.rules.models";
const RULES_EXCEPTIONS: &str = "sqltide.compiler.rules.exceptions";
const DIAGNOSTICS_MODELS: &str = "sqltide.diagnostics.models";
const DIAGNOSTICS_TYPES: &str = "sqltide.diagnostics.types";
const RELAXED_GC_THRESHOLDS: [i64; 3] = [2000, 100, 100];
const MAX_FIX_PASSES: usize = 128;

type Arguments<'a, 'py> = &'a [(&'a str, Bound<'py, PyAny>)];

fn native<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(NATIVE)?.getattr(name)
}

fn none(py: Python<'_>) -> Bound<'_, PyAny> {
    py.None().into_bound(py)
}

fn object<'py, T: IntoPyObjectExt<'py>>(py: Python<'py>, value: T) -> PyResult<Bound<'py, PyAny>> {
    value.into_bound_py_any(py)
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

/// `dataclasses.replace(value, **changes)`.
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

/// `value.name`, or `None` when `value` is `None`.
fn optional<'py>(value: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        Ok(none(value.py()))
    } else {
        value.getattr(name)
    }
}

fn items<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    value.try_iter()?.collect()
}

fn tuple<'py>(py: Python<'py>, values: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

fn elapsed_ms(start: Instant) -> u128 {
    start.elapsed().as_millis()
}

/// The authored file of one table or view model, else `None`.
fn model_source_path<'py>(model: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = model.py();
    let models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    if model.is_instance(&models.getattr("CompiledTableModel")?)? {
        return model.getattr("transform")?.getattr("source_file_path");
    }
    if model.is_instance(&models.getattr("CompiledViewModel")?)? {
        return model.getattr("view")?.getattr("source_file_path");
    }
    Ok(none(py))
}

/// Attach resolved target metadata and every discovered resource to one compile snapshot.
#[pyfunction]
#[pyo3(signature = (*, discovered_inputs, adapter_profile))]
fn build_compile_inputs<'py>(
    py: Python<'py>,
    discovered_inputs: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let loaded_project: Bound<'py, PyAny> = discovered_inputs.getattr("loaded_project")?;
    let project: Bound<'py, PyAny> = optional(&loaded_project, "project")?;
    let configuration: Bound<'py, PyAny> = optional(&loaded_project, "effective_configuration")?;
    let target_metadata: Bound<'py, PyAny> = adapter_profile.getattr("target_metadata")?;
    let project_database: Bound<'py, PyAny> = optional(&project, "default_database")?;
    let effective_target: Bound<'py, PyAny> = build(
        py,
        COMPILE_MODELS,
        "CompilerTargetMetadata",
        &[
            (
                "default_database",
                if project_database.is_none() {
                    target_metadata.getattr("default_database")?
                } else {
                    project_database
                },
            ),
            ("default_schema", target_metadata.getattr("default_schema")?),
            (
                "production_target",
                if configuration.is_none() {
                    object(py, false)?
                } else {
                    configuration.getattr("production_target")?
                },
            ),
            (
                "destruction_relation_drop_size_limit",
                if configuration.is_none() {
                    none(py)
                } else {
                    configuration
                        .getattr("destruction")?
                        .getattr("max_table_size_to_drop_bytes")?
                },
            ),
        ],
    )?;
    let variables: Bound<'py, PyAny> = if configuration.is_none() {
        PyTuple::empty(py).into_any()
    } else {
        configuration.getattr("variables")?
    };
    let pipeline_modes: Bound<'py, PyAny> = py.import(DISCOVERY_TYPES)?.getattr("PipelineMode")?;
    let virtual_environments: bool = !configuration.is_none()
        && configuration
            .getattr("defaults")?
            .getattr("pipeline_mode")?
            .eq(pipeline_modes.getattr("VIRTUAL")?)?;
    let authored_registry: Bound<'py, PyAny> = optional(&loaded_project, "macro_registry")?;
    let base_registry: Bound<'py, PyAny> = if authored_registry.is_none() {
        call(
            &wrap_pyfunction!(crate::macros::load_macro_registry, py)?.into_any(),
            &[("macro_files", discovered_inputs.getattr("macro_files")?)],
        )?
    } else {
        authored_registry
    };
    let declarations: Bound<'py, PyAny> = call(
        &native(py, "load_declarations")?,
        &[
            (
                "constant_files",
                discovered_inputs.getattr("constant_files")?,
            ),
            ("enum_files", discovered_inputs.getattr("enum_files")?),
        ],
    )?;
    let macro_registry: Bound<'py, PyAny> = replace(
        &base_registry,
        &[
            ("declarations", declarations),
            ("expansions", PyDict::new(py).into_any()),
        ],
    )?;
    let macro_context: Bound<'py, PyAny> = build(
        py,
        MACRO_MODELS,
        "MacroContext",
        &[
            (
                "adapter_name",
                adapter_profile.getattr("identity")?.getattr("name")?,
            ),
            ("dialect", adapter_profile.getattr("sql_analysis_dialect")?),
            ("target_name", optional(&configuration, "target_name")?),
            ("database", effective_target.getattr("default_database")?),
            ("schema", effective_target.getattr("default_schema")?),
            ("virtual_environments", object(py, virtual_environments)?),
            ("variables", py.get_type::<PyDict>().call1((&variables,))?),
        ],
    )?;
    let source_files: Bound<'py, PyAny> = discovered_inputs.getattr("source_files")?;
    let sources_by_name: Bound<'py, PyAny> =
        native(py, "source_registry_by_name")?.call1((&source_files,))?;
    let pipelines: Bound<'py, PyAny> = call(
        &native(py, "load_discovered_pipelines")?,
        &[
            (
                "pipeline_directories",
                discovered_inputs.getattr("pipeline_directories")?,
            ),
            ("model_files", discovered_inputs.getattr("model_files")?),
            ("macro_registry", macro_registry.clone()),
            ("macro_context", macro_context.clone()),
            ("sources_by_name", sources_by_name.clone()),
            ("project", project.clone()),
            (
                "project_naming",
                if configuration.is_none() {
                    build(py, DISCOVERY_MODELS, "ProjectNaming", &[])?
                } else {
                    configuration.getattr("naming")?
                },
            ),
            (
                "default_mode",
                if configuration.is_none() {
                    pipeline_modes.getattr("DIRECT")?
                } else {
                    pipeline_modes.call1((configuration
                        .getattr("defaults")?
                        .getattr("pipeline_mode")?,))?
                },
            ),
        ],
    )?;
    let contents_by_path: Bound<'py, PyDict> = PyDict::new(py);
    for files in ["test_files", "audit_files"] {
        for source_file in items(&discovered_inputs.getattr(files)?)? {
            contents_by_path.set_item(
                source_file.getattr("file_path")?,
                source_file.getattr("contents")?,
            )?;
        }
    }
    let project_dir: Bound<'py, PyAny> = discovered_inputs.getattr("project_dir")?;
    let tests: Bound<'py, PyAny> = py.get_type::<PyTuple>().call1((call(
        &native(py, "discover_sql_tests")?,
        &[
            ("root", project_dir.div("tests")?),
            ("contents_by_path", contents_by_path.clone().into_any()),
            ("macro_registry", macro_registry.clone()),
            ("macro_context", macro_context.clone()),
        ],
    )?,))?;
    let mut model_steps: Vec<Bound<'py, PyAny>> = Vec::new();
    for loaded_pipeline in items(&pipelines)? {
        model_steps.extend(items(
            &loaded_pipeline.getattr("pipeline")?.getattr("transforms")?,
        )?);
    }
    let root: Bound<'py, PyAny> = project_dir.div("audits")?;
    let generic_audit_instances: Bound<'py, PyAny> = call(
        &native(py, "build_model_audit_instances")?,
        &[("models", tuple(py, model_steps)?)],
    )?;
    let audits: Bound<'py, PyAny> = py.get_type::<PyTuple>().call1((call(
        &native(py, "discover_sql_audits")?,
        &[
            ("root", root),
            ("contents_by_path", contents_by_path.into_any()),
            ("macro_registry", macro_registry.clone()),
            ("macro_context", macro_context.clone()),
            ("generic_audit_instances", generic_audit_instances),
        ],
    )?,))?;
    let _ = call(
        &native(py, "validate_attached_project_inputs")?,
        &[
            ("source_files", source_files),
            ("loaded_project", loaded_project),
            ("loaded_pipelines", pipelines.clone()),
            ("loaded_tests", tests.clone()),
            ("loaded_audits", audits.clone()),
        ],
    )?;
    let sorted_names: Bound<'py, PyAny> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((&sources_by_name,))?;
    let sources: Vec<Bound<'py, PyAny>> = items(&sorted_names)?
        .iter()
        .map(|name| sources_by_name.get_item(name))
        .collect::<PyResult<_>>()?;
    let (model_reference_scope, allowed_cross_pipeline_references) = if !configuration.is_none() {
        let dependencies: Bound<'py, PyAny> = configuration.getattr("dependencies")?;
        (
            dependencies.getattr("model_reference_scope")?,
            dependencies.getattr("allowed_cross_pipeline_references")?,
        )
    } else if !project.is_none() {
        let dependencies: Bound<'py, PyAny> = project.getattr("dependencies")?;
        (
            dependencies.getattr("model_reference_scope")?,
            dependencies.getattr("allowed_cross_pipeline_references")?,
        )
    } else {
        (
            py.import(DISCOVERY_TYPES)?
                .getattr("ModelReferenceScope")?
                .getattr("PROJECT")?,
            PyTuple::empty(py).into_any(),
        )
    };
    build(
        py,
        COMPILE_MODELS,
        "CompileProjectInputs",
        &[
            ("discovered_inputs", discovered_inputs.clone()),
            ("adapter_profile", adapter_profile.clone()),
            ("effective_target", effective_target),
            ("variables", variables),
            ("macro_registry", macro_registry),
            ("macro_context", macro_context),
            ("pipelines", pipelines),
            ("tests", tests),
            ("audits", audits),
            ("sources", tuple(py, sources)?),
            ("virtual_environments", object(py, virtual_environments)?),
            ("project_name", optional(&configuration, "name")?),
            ("target_name", optional(&configuration, "target_name")?),
            ("model_reference_scope", model_reference_scope),
            (
                "allowed_cross_pipeline_references",
                allowed_cross_pipeline_references,
            ),
            (
                "consumer_group_identity",
                optional(&configuration, "consumer_group_identity")?,
            ),
        ],
    )
}

/// One project phase's diagnostic location: the project file, else the pipelines root.
fn project_location<'py>(
    pipelines_root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = pipelines_root.py();
    let path: Bound<'py, PyAny> = if loaded_project.is_none() {
        pipelines_root.clone()
    } else {
        loaded_project
            .getattr("source_file")?
            .getattr("file_path")?
    };
    build(
        py,
        DIAGNOSTICS_MODELS,
        "SourceLocation",
        &[
            ("path", path),
            ("line", object(py, 1)?),
            ("column", object(py, 1)?),
        ],
    )
}

/// Run one phase, attaching its diagnostic to any `Exception` before it propagates.
fn phase<'py, T>(
    pipelines_root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
    diagnostic_phase: &str,
    code: &str,
    run: impl FnOnce() -> PyResult<T>,
) -> PyResult<T> {
    let py: Python<'py> = pipelines_root.py();
    run().or_else(|failure| {
        if failure.is_instance_of::<PyException>(py) {
            crate::cli::render::attach_error_diagnostic(
                py,
                failure.value(py).as_any(),
                py.import(DIAGNOSTICS_TYPES)?
                    .getattr("DiagnosticPhase")?
                    .getattr(diagnostic_phase)?,
                code,
                py.None().into_bound(py),
                project_location(pipelines_root, loaded_project)?,
            )?;
        }
        Err(failure)
    })
}

/// Run `work` with generational collection deferred, since retained parse trees hold no cycles.
fn with_deferred_collection<'py, T>(
    py: Python<'py>,
    work: impl FnOnce() -> PyResult<T>,
) -> PyResult<T> {
    let gc: Bound<'py, PyModule> = py.import("gc")?;
    let previous: Bound<'py, PyAny> = gc.call_method0("get_threshold")?;
    let thresholds: Vec<i64> = previous.extract()?;
    if thresholds.len() != RELAXED_GC_THRESHOLDS.len() {
        return Err(PyValueError::new_err(
            "zip() argument 2 is shorter than argument 1",
        ));
    }
    let relaxed: Vec<i64> = thresholds
        .iter()
        .zip(RELAXED_GC_THRESHOLDS)
        .map(|(previous, relaxed)| (*previous).max(relaxed))
        .collect();
    let _ = gc.call_method1("set_threshold", PyTuple::new(py, relaxed)?)?;
    let result: PyResult<T> = work();
    let _ = gc.call_method1("set_threshold", previous.cast_into::<PyTuple>()?)?;
    result
}

/// Retain discovery ownership while replacing model, test and audit source text.
fn override_sources<'py>(
    inputs: &Bound<'py, PyAny>,
    overrides: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = inputs.py();
    let mut changes: Vec<(&str, Bound<'py, PyAny>)> = Vec::new();
    for files in ["model_files", "test_files", "audit_files"] {
        let replaced: Vec<Bound<'py, PyAny>> = items(&inputs.getattr(files)?)?
            .iter()
            .map(|source| {
                let contents: Bound<'py, PyAny> = overrides.call_method1(
                    "get",
                    (source.getattr("file_path")?, source.getattr("contents")?),
                )?;
                replace(source, &[("contents", contents)])
            })
            .collect::<PyResult<_>>()?;
        changes.push((files, tuple(py, replaced)?));
    }
    replace(inputs, &changes)
}

/// Pipelines, models, tests and audits share one namespace with sensors.
fn reserved_sensor_names<'py>(compile_inputs: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = compile_inputs.py();
    let names: Bound<'py, PySet> = PySet::empty(py)?;
    for loaded_pipeline in items(&compile_inputs.getattr("pipelines")?)? {
        let pipeline: Bound<'py, PyAny> = loaded_pipeline.getattr("pipeline")?;
        names.add(pipeline.getattr("name")?)?;
        for transform in items(&pipeline.getattr("transforms")?)? {
            names.add(transform.getattr("name")?)?;
        }
    }
    for resources in ["tests", "audits"] {
        for resource in items(&compile_inputs.getattr(resources)?)? {
            let name: Bound<'py, PyAny> = resource.getattr("name")?;
            if !name.is_none() {
                names.add(name)?;
            }
        }
    }
    py.get_type::<PyFrozenSet>().call1((names,))
}

/// Discover, attach, compile, graph and realize one project without opening a connection.
#[pyfunction]
#[pyo3(signature = (
    *, pipelines_root, loaded_project, adapter_profile, enforce_rules = true,
    source_overrides = None, rule_selection = None
))]
pub(crate) fn analyze_project<'py>(
    py: Python<'py>,
    pipelines_root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
    enforce_rules: bool,
    source_overrides: Option<Bound<'py, PyAny>>,
    rule_selection: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let discovery_start: Instant = Instant::now();
    let discovered_inputs: Bound<'py, PyAny> = phase(
        pipelines_root,
        loaded_project,
        "DISCOVERY",
        "SQT-DISCOVERY-001",
        || {
            let discovered: Bound<'py, PyAny> = call(
                &native(py, "discover_project_inputs")?,
                &[
                    ("pipelines_root", pipelines_root.clone()),
                    ("loaded_project", loaded_project.clone()),
                ],
            )?;
            match &source_overrides {
                Some(overrides) if overrides.is_truthy()? => {
                    override_sources(&discovered, overrides)
                }
                _ => Ok(discovered),
            }
        },
    )?;
    let discovery_ms: u128 = elapsed_ms(discovery_start);
    let compile_inputs_start: Instant = Instant::now();
    let (compile_inputs, access_policy, sensors) = phase(
        pipelines_root,
        loaded_project,
        "DISCOVERY",
        "SQT-DISCOVERY-001",
        || {
            let compile_inputs: Bound<'py, PyAny> = call(
                &native(py, "build_compile_inputs")?,
                &[
                    ("discovered_inputs", discovered_inputs.clone()),
                    ("adapter_profile", adapter_profile.clone()),
                ],
            )?;
            let access_file: Bound<'py, PyAny> = discovered_inputs.getattr("access_file")?;
            let access_policy: Bound<'py, PyAny> = if access_file.is_none() {
                none(py)
            } else {
                let pipeline_names: Vec<Bound<'py, PyAny>> =
                    items(&compile_inputs.getattr("pipelines")?)?
                        .iter()
                        .map(|loaded| loaded.getattr("pipeline")?.getattr("name"))
                        .collect::<PyResult<_>>()?;
                native(py, "compile_access_policy")?.call1((
                    access_file.getattr("contents")?,
                    access_file.getattr("file_path")?,
                    PyFrozenSet::new(py, pipeline_names)?,
                ))?
            };
            let sensors: Bound<'py, PyAny> = call(
                &native(py, "compile_sensors")?,
                &[
                    ("project_dir", pipelines_root.getattr("parent")?),
                    ("reserved_names", reserved_sensor_names(&compile_inputs)?),
                ],
            )?;
            Ok((compile_inputs, access_policy, sensors))
        },
    )?;
    let compile_inputs_ms: u128 = elapsed_ms(compile_inputs_start);
    let sql_analyzer: Bound<'py, PyAny> = call(
        &native(py, "SqlModelAnalyzer")?,
        &[(
            "dialect",
            adapter_profile
                .getattr("type_inference_profile")?
                .getattr("sql_analysis_dialect")?,
        )],
    )?;
    let reference_rewriter: Bound<'py, PyAny> = call(
        &native(py, "SqlReferenceRewriter")?,
        &[("dialect", adapter_profile.getattr("sql_analysis_dialect")?)],
    )?;
    let assembly_start: Instant = Instant::now();
    let compiled_project: Bound<'py, PyAny> = phase(
        pipelines_root,
        loaded_project,
        "COMPILATION",
        "SQT-COMPILE-001",
        || {
            with_deferred_collection(py, || {
                call(
                    &native(py, "assemble_project")?,
                    &[
                        ("inputs", compile_inputs.clone()),
                        ("reference_rewriter", reference_rewriter),
                        ("sql_analyzer", sql_analyzer.clone()),
                    ],
                )
            })
        },
    )?;
    let assembly_ms: u128 = elapsed_ms(assembly_start);
    let graph_start: Instant = Instant::now();
    let graph: Bound<'py, PyAny> = phase(
        pipelines_root,
        loaded_project,
        "GRAPH",
        "SQT-GRAPH-001",
        || {
            call(
                &native(py, "build_project_graph_from_compiled_project")?,
                &[("project", compiled_project.clone())],
            )
        },
    )?;
    let graph_ms: u128 = elapsed_ms(graph_start);
    let realization_start: Instant = Instant::now();
    let realized_project: Bound<'py, PyAny> = phase(
        pipelines_root,
        loaded_project,
        "REALIZATION",
        "SQT-REALIZATION-001",
        || {
            with_deferred_collection(py, || {
                call(
                    &native(py, "realize_project")?,
                    &[
                        ("project", compiled_project.clone()),
                        ("adapter_profile", adapter_profile.clone()),
                        ("sql_analyzer", sql_analyzer),
                    ],
                )
            })
        },
    )?;
    let realization_ms: u128 = elapsed_ms(realization_start);
    let timings: Bound<'py, PyAny> = build(
        py,
        PIPELINE_MODELS,
        "CompilationTimings",
        &[
            ("discovery_ms", object(py, discovery_ms)?),
            ("compile_inputs_ms", object(py, compile_inputs_ms)?),
            ("assembly_ms", object(py, assembly_ms)?),
            ("graph_ms", object(py, graph_ms)?),
            ("realization_ms", object(py, realization_ms)?),
        ],
    )?;
    let analysis: Bound<'py, PyAny> = build(
        py,
        PIPELINE_MODELS,
        "CompileAnalysis",
        &[
            ("discovered_inputs", discovered_inputs),
            ("compile_inputs", compile_inputs),
            ("adapter_profile", adapter_profile.clone()),
            ("compiled_project", compiled_project),
            ("realized_project", realized_project),
            ("graph", graph),
            ("diagnostics", PyTuple::empty(py).into_any()),
            ("timings", timings),
            ("access_policy", access_policy),
            ("sensors", sensors),
        ],
    )?;
    evaluate_project_rules(py, &analysis, enforce_rules, rule_selection)
}

fn rules_config<'py>(
    py: Python<'py>,
    loaded_project: &Bound<'py, PyAny>,
    selection: &Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    call(
        &native(py, "rules_config")?,
        &[
            ("loaded_project", loaded_project.clone()),
            ("selection", selection.clone().unwrap_or_else(|| none(py))),
        ],
    )
}

fn rules_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        py.import(RULES_EXCEPTIONS)?
            .getattr("SqlRulesError")?
            .call1((message,))?,
    ))
}

/// Resolve ignore scopes from compiler-owned models: named pipelines own their models, and
/// downstream resources retain their checks.
fn resolve_rule_ignores<'py>(
    config: Bound<'py, PyAny>,
    compiled_project: &Bound<'py, PyAny>,
    project_dir: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = config.py();
    let scopes: Vec<Bound<'py, PyAny>> = items(&config.call_method0("ignore_selectors")?)?;
    let mut any_scope: bool = false;
    for scope in &scopes {
        if scope.is_truthy()? {
            any_scope = true;
            break;
        }
    }
    if !any_scope {
        return Ok(config);
    }
    let paths_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for model in items(&compiled_project.getattr("models")?)? {
        let path: Bound<'py, PyAny> = model_source_path(&model)?;
        if !path.is_none() {
            paths_by_key.set_item(
                model.getattr("key")?,
                path.call_method1("relative_to", (project_dir,))?
                    .call_method0("as_posix")?,
            )?;
        }
    }
    let resolved: Bound<'py, PyList> = PyList::empty(py);
    for selectors in scopes {
        if !selectors.is_truthy()? {
            resolved.append(PyList::empty(py))?;
            continue;
        }
        let keys: Bound<'py, PyFrozenSet> = resolve_named_model_keys(
            py,
            &compiled_project.getattr("pipelines")?,
            &py.get_type::<PyTuple>().call1((selectors,))?,
        )?;
        let paths: Bound<'py, PyList> = PyList::empty(py);
        for key in keys.iter() {
            if let Some(path) = paths_by_key.get_item(&key)? {
                paths.append(path)?;
            }
        }
        if paths.is_empty() {
            return Err(rules_error(
                py,
                "Rule ignore selectors must resolve to authored model files",
            )?);
        }
        resolved.append(paths)?;
    }
    config.call_method1("with_selector_paths", (resolved,))
}

/// Attach timed authored Rule findings, or fail before any artifacts are written.
fn evaluate_project_rules<'py>(
    py: Python<'py>,
    analysis: &Bound<'py, PyAny>,
    enforce: bool,
    selection: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let start: Instant = Instant::now();
    let discovered: Bound<'py, PyAny> = analysis.getattr("discovered_inputs")?;
    let project_dir: Bound<'py, PyAny> = discovered.getattr("project_dir")?;
    let config: Bound<'py, PyAny> = resolve_rule_ignores(
        rules_config(py, &discovered.getattr("loaded_project")?, &selection)?,
        &analysis.getattr("compiled_project")?,
        &project_dir,
    )?;
    let adapter_profile: Bound<'py, PyAny> = analysis.getattr("adapter_profile")?;
    let findings: Bound<'py, PyAny> = if adapter_profile.getattr("run_sql_rules")?.is_truthy()?
        && config.call_method0("selected_rules")?.is_truthy()?
    {
        call(
            &native(py, "evaluate_rules")?,
            &[
                ("inputs", analysis.getattr("compile_inputs")?),
                ("config", config.clone()),
                ("dialect", adapter_profile.getattr("sql_analysis_dialect")?),
                ("project_dir", project_dir),
            ],
        )?
    } else {
        PyTuple::empty(py).into_any()
    };
    if enforce && findings.is_truthy()? {
        return Err(PyErr::from_value(build(
            py,
            RULES_EXCEPTIONS,
            "RulesError",
            &[
                ("findings", findings),
                ("defaults", object(py, config.getattr("select")?.is_none())?),
            ],
        )?));
    }
    let diagnostics: Vec<Bound<'py, PyAny>> = items(&findings)?
        .iter()
        .map(|finding| finding.getattr("diagnostic"))
        .collect::<PyResult<_>>()?;
    let diagnostics: Bound<'py, PyAny> = tuple(py, diagnostics)?;
    let timings: Bound<'py, PyAny> = replace(
        &analysis.getattr("timings")?,
        &[("rules_ms", object(py, elapsed_ms(start))?)],
    )?;
    replace(
        analysis,
        &[("diagnostics", diagnostics), ("timings", timings)],
    )
}

/// `Counter((path, code, message) for each diagnostic)`.
fn diagnostic_counts<'py>(analysis: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = analysis.py();
    let keys: Vec<Bound<'py, PyAny>> = items(&analysis.getattr("diagnostics")?)?
        .iter()
        .map(|item| {
            let location: Bound<'py, PyAny> = item.getattr("location")?;
            let path: Bound<'py, PyAny> = if location.is_truthy()? {
                location.getattr("path")?
            } else {
                none(py)
            };
            tuple(
                py,
                vec![path, item.getattr("code")?, item.getattr("message")?],
            )
        })
        .collect::<PyResult<_>>()?;
    py.import("collections")?
        .getattr("Counter")?
        .call1((PyList::new(py, keys)?,))
}

fn models_by_key<'py>(analysis: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let models: Bound<'py, PyDict> = PyDict::new(analysis.py());
    for model in items(&analysis.getattr("compiled_project")?.getattr("models")?)? {
        models.set_item(model.getattr("key")?, model)?;
    }
    Ok(models)
}

/// Reject a Rule fix that changed columns, dependencies, replay lineage, another model's SQL or
/// another file's diagnostics.
fn verify_fix<'py>(
    before: &Bound<'py, PyAny>,
    after: &Bound<'py, PyAny>,
    edited: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py: Python<'py> = before.py();
    let previous: Bound<'py, PyDict> = models_by_key(before)?;
    let current: Bound<'py, PyDict> = models_by_key(after)?;
    if previous
        .call_method0("keys")?
        .ne(current.call_method0("keys")?)?
        || before
            .getattr("graph")?
            .getattr("upstream_edges_by_key")?
            .ne(after.getattr("graph")?.getattr("upstream_edges_by_key")?)?
    {
        return Err(rules_error(
            py,
            "Rule fix changed project models or dependencies",
        )?);
    }
    let table_class: Bound<'py, PyAny> =
        py.import(COMPILE_MODELS)?.getattr("CompiledTableModel")?;
    for (key, model) in previous.iter() {
        let other: Bound<'py, PyAny> = current.as_any().get_item(&key)?;
        if model
            .getattr("output_columns")?
            .ne(other.getattr("output_columns")?)?
            || model.getattr("kind")?.ne(other.getattr("kind")?)?
        {
            return Err(rules_error(
                py,
                "Rule fix changed output names, types or nullability",
            )?);
        }
        let analysis: Bound<'py, PyAny> = model.getattr("sql_analysis")?;
        let other_analysis: Bound<'py, PyAny> = other.getattr("sql_analysis")?;
        if analysis
            .getattr("storage_expressions")?
            .ne(other_analysis.getattr("storage_expressions")?)?
            || analysis
                .getattr("aggregate_facts")?
                .getattr("has_semantics")?
                .ne(other_analysis
                    .getattr("aggregate_facts")?
                    .getattr("has_semantics")?)?
        {
            return Err(rules_error(
                py,
                "Rule fix changed storage or aggregation semantics",
            )?);
        }
        if model.is_instance(&table_class)? && other.is_instance(&table_class)? {
            for lineage in [
                "preserves_required_lineage",
                "effective_bounded_replay_fallback",
                "replay_anchor_eligible",
            ] {
                if model.getattr(lineage)?.ne(other.getattr(lineage)?)? {
                    return Err(rules_error(py, "Rule fix changed replay lineage")?);
                }
            }
        }
        if model_source_path(&model)?.ne(edited)? {
            let dialect: String = before
                .getattr("adapter_profile")?
                .getattr("sql_analysis_dialect")?
                .extract()?;
            let old_sql: String =
                sql_canonicalize(&model.getattr("query")?.extract::<String>()?, &dialect)?;
            let new_sql: String =
                sql_canonicalize(&other.getattr("query")?.extract::<String>()?, &dialect)?;
            if old_sql != new_sql {
                return Err(rules_error(py, "Rule fix changed another model's SQL")?);
            }
        }
    }
    let previous_diagnostics: Bound<'py, PyAny> = diagnostic_counts(before)?;
    let current_diagnostics: Bound<'py, PyAny> = diagnostic_counts(after)?;
    let mut changed: bool = current_diagnostics
        .sub(&previous_diagnostics)?
        .is_truthy()?;
    if !changed {
        for entry in items(&previous_diagnostics.call_method0("items")?)? {
            let (key, count): (Bound<'py, PyAny>, Bound<'py, PyAny>) = entry.extract()?;
            if key.get_item(0)?.ne(edited)? && count.ne(current_diagnostics.get_item(&key)?)? {
                changed = true;
                break;
            }
        }
    }
    if changed {
        return Err(rules_error(
            py,
            "Rule fix introduced diagnostics or changed another file's diagnostics",
        )?);
    }
    Ok(())
}

fn analyze_for_rules<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
    selection: &Option<Bound<'py, PyAny>>,
    source_overrides: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    analyze_project(
        py,
        root,
        loaded_project,
        adapter_profile,
        false,
        source_overrides,
        selection.clone(),
    )
}

fn one_override<'py>(
    path: &Bound<'py, PyAny>,
    contents: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let overrides: Bound<'py, PyDict> = PyDict::new(path.py());
    overrides.set_item(path, contents)?;
    Ok(overrides.into_any())
}

/// The source with only allowlisted, compiler-verified Rule edits applied and formatted, and the
/// file's remaining diagnostics.
#[allow(clippy::too_many_arguments)]
fn fixed_file<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    keyword: &str,
    root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
    selection: &Option<Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let path: Bound<'py, PyAny> = source.getattr("file_path")?;
    let mut candidate: Bound<'py, PyAny> = source.getattr("contents")?;
    let config: Bound<'py, PyAny> = rules_config(py, loaded_project, selection)?;
    let baseline: Bound<'py, PyAny> =
        analyze_for_rules(py, root, loaded_project, adapter_profile, selection, None)?;
    let config: Bound<'py, PyAny> = resolve_rule_ignores(
        config,
        &baseline.getattr("compiled_project")?,
        &root.getattr("parent")?,
    )?;
    let dialect: Bound<'py, PyAny> = adapter_profile.getattr("sql_analysis_dialect")?;
    let mut seen: HashSet<String> = HashSet::new();
    for _ in 0..MAX_FIX_PASSES {
        let text: String = candidate.extract()?;
        if !seen.insert(text) {
            return Err(rules_error(
                py,
                &format!(
                    "{}: Rule fixes cycle; original source preserved",
                    path.str()?
                ),
            )?);
        }
        let current: Bound<'py, PyAny> = analyze_for_rules(
            py,
            root,
            loaded_project,
            adapter_profile,
            selection,
            Some(one_override(&path, &candidate)?),
        )?;
        verify_fix(&baseline, &current, &path)?;
        let findings: Vec<Bound<'py, PyAny>> =
            if adapter_profile.getattr("run_sql_rules")?.is_truthy()? {
                items(&call(
                    &native(py, "evaluate_rules")?,
                    &[
                        ("inputs", current.getattr("compile_inputs")?),
                        ("config", config.clone()),
                        ("dialect", dialect.clone()),
                        ("project_dir", root.getattr("parent")?),
                        ("only_path", path.clone()),
                        ("check_stale", object(py, false)?),
                    ],
                )?)?
            } else {
                Vec::new()
            };
        let mut edits: Vec<(i64, Bound<'py, PyAny>)> = Vec::new();
        for finding in findings {
            if !finding.getattr("replacement")?.is_none() {
                let start: Bound<'py, PyAny> = finding.getattr("fix_start")?;
                let order: i64 = if start.is_truthy()? {
                    start.extract()?
                } else {
                    0
                };
                edits.push((order, finding));
            }
        }
        edits.sort_by_key(|(order, _)| *order);
        let Some((_, edit)) = edits.into_iter().next() else {
            let formatted: Bound<'py, PyAny> = call(
                &native(py, "formatted_file")?,
                &[
                    ("source", replace(source, &[("contents", candidate)])?),
                    ("keyword", object(py, keyword)?),
                    ("dialect", dialect),
                ],
            )?;
            let last: Bound<'py, PyAny> = analyze_for_rules(
                py,
                root,
                loaded_project,
                adapter_profile,
                selection,
                Some(one_override(&path, &formatted)?),
            )?;
            verify_fix(&baseline, &last, &path)?;
            let mut diagnostics: Vec<Bound<'py, PyAny>> = Vec::new();
            for diagnostic in items(&last.getattr("diagnostics")?)? {
                let location: Bound<'py, PyAny> = diagnostic.getattr("location")?;
                if !location.is_none() && location.getattr("path")?.eq(&path)? {
                    diagnostics.push(diagnostic);
                }
            }
            return Ok((formatted, tuple(py, diagnostics)?));
        };
        let fix_start: Bound<'py, PyAny> = edit.getattr("fix_start")?;
        let fix_end: Bound<'py, PyAny> = edit.getattr("fix_end")?;
        let replacement: Bound<'py, PyAny> = edit.getattr("replacement")?;
        if fix_start.is_none() || fix_end.is_none() || replacement.is_none() {
            return Err(rules_error(
                py,
                &format!(
                    "{}: Rule edit is incomplete; original source preserved",
                    path.str()?
                ),
            )?);
        }
        let head: Bound<'py, PyAny> =
            candidate.get_item(PySlice::new(py, 0, fix_start.extract()?, 1))?;
        let tail: Bound<'py, PyAny> =
            candidate.get_item(PySlice::new(py, fix_end.extract()?, isize::MAX, 1))?;
        candidate = head.add(replacement)?.add(tail)?;
    }
    Err(rules_error(
        py,
        &format!(
            "{}: Rule fixes exceeded {MAX_FIX_PASSES} passes; original source preserved",
            path.str()?
        ),
    )?)
}

/// Formatted model, test and audit source candidates, without writing or opening a connection.
#[pyfunction]
#[pyo3(signature = (*, root, loaded_project, adapter_profile, selection, paths, fix))]
fn format_project_sql<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    loaded_project: &Bound<'py, PyAny>,
    adapter_profile: &Bound<'py, PyAny>,
    selection: Option<Bound<'py, PyAny>>,
    paths: Vec<Bound<'py, PyAny>>,
    fix: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let config: Bound<'py, PyAny> = rules_config(py, loaded_project, &selection)?;
    let mut ignores: bool = false;
    for scope in items(&config.call_method0("ignore_selectors")?)? {
        if scope.is_truthy()? {
            ignores = true;
            break;
        }
    }
    if ignores {
        let _ = analyze_for_rules(py, root, loaded_project, adapter_profile, &selection, None)?;
    }
    let discovered: Bound<'py, PyAny> = call(
        &native(py, "discover_project_inputs")?,
        &[
            ("pipelines_root", root.clone()),
            ("loaded_project", loaded_project.clone()),
        ],
    )?;
    let dialect: Bound<'py, PyAny> = adapter_profile.getattr("sql_analysis_dialect")?;
    let formatted_class: Bound<'py, PyAny> = py.import(RULES_MODELS)?.getattr("FormattedSource")?;
    let mut results: Vec<Bound<'py, PyAny>> = Vec::new();
    for (files, keyword) in [
        ("model_files", "MODEL"),
        ("test_files", "TEST"),
        ("audit_files", "AUDIT"),
    ] {
        for source in items(&discovered.getattr(files)?)? {
            let file_path: Bound<'py, PyAny> = source.getattr("file_path")?;
            if !paths.is_empty() {
                let resolved: Bound<'py, PyAny> = file_path.call_method0("resolve")?;
                let mut selected: bool = false;
                for path in &paths {
                    if resolved.eq(path)?
                        || resolved
                            .call_method1("is_relative_to", (path,))?
                            .is_truthy()?
                    {
                        selected = true;
                        break;
                    }
                }
                if !selected {
                    continue;
                }
            }
            let original: Bound<'py, PyAny> = source.getattr("contents")?;
            let outcome: PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = if fix {
                fixed_file(
                    py,
                    &source,
                    keyword,
                    root,
                    loaded_project,
                    adapter_profile,
                    &selection,
                )
            } else {
                call(
                    &native(py, "formatted_file")?,
                    &[
                        ("source", source.clone()),
                        ("keyword", object(py, keyword)?),
                        ("dialect", dialect.clone()),
                    ],
                )
                .map(|formatted| (formatted, PyTuple::empty(py).into_any()))
            };
            let result: Bound<'py, PyAny> = match outcome {
                Ok((formatted, diagnostics)) => call(
                    &formatted_class,
                    &[
                        ("path", file_path),
                        ("original", original),
                        ("formatted", formatted),
                        ("diagnostics", diagnostics),
                    ],
                )?,
                Err(failure) if failure.is_instance_of::<PyValueError>(py) => call(
                    &formatted_class,
                    &[
                        ("path", file_path),
                        ("original", original.clone()),
                        ("formatted", original),
                        ("error", failure.value(py).str()?.into_any()),
                    ],
                )?,
                Err(failure) => return Err(failure),
            };
            results.push(result);
        }
    }
    PyTuple::new(py, results)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_compile_inputs, module)?)?;
    module.add_function(wrap_pyfunction!(analyze_project, module)?)?;
    module.add_function(wrap_pyfunction!(format_project_sql, module)?)
}

#[cfg(test)]
mod tests {
    #[test]
    fn relaxed_thresholds_keep_three_generations() {
        assert_eq!(super::RELAXED_GC_THRESHOLDS.len(), 3);
    }
}
