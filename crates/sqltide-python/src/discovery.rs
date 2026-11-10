//! Configuration interpolation over Python values, and pipeline source inference.

use std::collections::{BTreeMap, HashMap, HashSet};

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyMapping, PyString, PyTuple};
use sqltide_discovery::interpolation::{self, Environment, Value};
use sqltide_discovery::project_validation::{
    ModelFacts, PipelineFacts, ProjectPolicyFacts, ReplayPolicyFacts,
};
use sqltide_discovery::sources::{PipelineTransforms, pipeline_sources};
use sqltide_discovery::sql_test_classification::{
    CteRole, classify_macro_test_ctes, classify_model_test_ctes,
};

/// Python objects that pass through interpolation untouched, by handle.
struct Values<'py> {
    py: Python<'py>,
    objects: Vec<Bound<'py, PyAny>>,
}

impl<'py> Values<'py> {
    /// Classify like the Python implementation: strings, then lists and tuples, then
    /// mappings; anything else passes through with its interpolation text.
    fn value(&mut self, object: &Bound<'py, PyAny>) -> PyResult<Value> {
        if let Ok(text) = object.downcast::<PyString>() {
            return Ok(Value::Str(text.to_str()?.to_owned()));
        }
        if object.is_instance_of::<PyList>() || object.is_instance_of::<PyTuple>() {
            return object
                .try_iter()?
                .map(|item| self.value(&item?))
                .collect::<PyResult<_>>()
                .map(Value::Seq);
        }
        if let Ok(mapping) = object.downcast::<PyMapping>() {
            return Ok(Value::Map(self.entries(mapping)?));
        }
        let text: String = if object.is_none() {
            "null".to_owned()
        } else if let Ok(flag) = object.downcast::<PyBool>() {
            if flag.is_true() { "true" } else { "false" }.to_owned()
        } else {
            object.str()?.to_str()?.to_owned()
        };
        self.objects.push(object.clone());
        Ok(Value::Other {
            handle: self.objects.len() - 1,
            text,
        })
    }

    fn entries(&mut self, mapping: &Bound<'py, PyMapping>) -> PyResult<Vec<(String, Value)>> {
        mapping
            .items()?
            .iter()
            .map(|item| {
                let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item.extract()?;
                let key: String = key
                    .downcast::<PyString>()
                    .map_err(|_| {
                        PyTypeError::new_err("configuration mapping keys must be strings")
                    })?
                    .to_str()?
                    .to_owned();
                Ok((key, self.value(&value)?))
            })
            .collect()
    }

    fn variables(&mut self, mapping: &Bound<'py, PyAny>) -> PyResult<BTreeMap<String, Value>> {
        Ok(self
            .entries(mapping.downcast::<PyMapping>()?)?
            .into_iter()
            .collect())
    }

    fn object(&self, value: Value) -> PyResult<Bound<'py, PyAny>> {
        Ok(match value {
            Value::Str(text) => PyString::new(self.py, &text).into_any(),
            Value::Seq(items) => PyTuple::new(
                self.py,
                items
                    .into_iter()
                    .map(|item| self.object(item))
                    .collect::<PyResult<Vec<_>>>()?,
            )?
            .into_any(),
            Value::Map(entries) => {
                let dict: Bound<'py, PyDict> = PyDict::new(self.py);
                for (key, item) in entries {
                    dict.set_item(key, self.object(item)?)?;
                }
                dict.into_any()
            }
            Value::Other { handle, .. } => self.objects[handle].clone(),
        })
    }
}

/// The caller's environment mapping, read with `in` and then indexing.
pub(crate) struct PyEnvironment<'py>(pub(crate) Bound<'py, PyAny>);

impl Environment for PyEnvironment<'_> {
    fn get(&self, name: &str) -> Option<String> {
        if !self.0.contains(name).unwrap_or(false) {
            return None;
        }
        let value: Bound<'_, PyAny> = self.0.get_item(name).ok()?;
        value.str().ok().map(|text| text.to_string())
    }
}

/// The `ProjectConfigError(message)` for one interpolation failure, raised `from None`.
fn config_error(py: Python<'_>, message: String) -> PyErr {
    let raised = || -> PyResult<PyErr> {
        let context: Bound<'_, PyAny> = PyValueError::new_err(message.clone())
            .value(py)
            .clone()
            .into_any();
        let error: Bound<'_, PyAny> = py
            .import("sqltide.compiler.discovery.exceptions")?
            .getattr("ProjectConfigError")?
            .call1((message,))?;
        error.setattr("__context__", context)?;
        error.setattr("__cause__", py.None())?;
        Ok(PyErr::from_value(error))
    };
    raised().unwrap_or_else(|error| error)
}

/// Resolve every project variable, returned in the order they finish resolving. Raises
/// `ProjectConfigError` with the message.
#[pyfunction]
#[pyo3(signature = (*, values, environment, field_path_prefix, defer_missing_environment=false))]
fn resolve_variable_values<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
    environment: Bound<'py, PyAny>,
    field_path_prefix: &str,
    defer_missing_environment: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let mut objects: Values<'py> = Values {
        py,
        objects: Vec::new(),
    };
    let values: BTreeMap<String, Value> = objects.variables(values)?;
    let resolved: Vec<(String, Value)> = interpolation::resolve_variable_values(
        &values,
        &PyEnvironment(environment),
        field_path_prefix,
        defer_missing_environment,
    )
    .map_err(|message| config_error(py, message))?;
    let dict: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in resolved {
        dict.set_item(name, objects.object(value)?)?;
    }
    Ok(dict)
}

/// Interpolate one configuration value. Raises `ProjectConfigError` with the message.
#[pyfunction]
#[pyo3(signature = (*, value, variables, environment, field_path))]
fn interpolate_config_value<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    variables: &Bound<'py, PyAny>,
    environment: Bound<'py, PyAny>,
    field_path: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let mut objects: Values<'py> = Values {
        py,
        objects: Vec::new(),
    };
    let value: Value = objects.value(value)?;
    let variables: BTreeMap<String, Value> = objects.variables(variables)?;
    let interpolated: Value = interpolation::interpolate_config_value(
        &value,
        &variables,
        &PyEnvironment(environment),
        field_path,
    )
    .map_err(|message| config_error(py, message))?;
    objects.object(interpolated)
}

/// The source name of each `(directory, [(transform, driving input)])` pipeline, up to the
/// first failing one, and that pipeline's error message.
#[pyfunction]
pub(crate) fn infer_pipeline_sources(
    pipelines: Vec<(String, Vec<(String, String)>)>,
    inputs_by_transform: HashMap<String, String>,
    sources: HashSet<String>,
) -> (Vec<Option<String>>, Option<String>) {
    let pipelines: Vec<PipelineTransforms> = pipelines
        .into_iter()
        .map(|(directory, transforms)| PipelineTransforms {
            directory,
            transforms,
        })
        .collect();
    pipeline_sources(&pipelines, &inputs_by_transform, &sources)
}

/// `(name, is table, defines replay_on_change, defines bounded_replay_fallback)`.
pub(crate) type ModelFactsArgument = (String, bool, bool, bool);
/// `(name, file path, is virtual, defines replay_on_change, defines bounded_replay_fallback,
/// models)`.
pub(crate) type PipelineFactsArgument = (String, String, bool, bool, bool, Vec<ModelFactsArgument>);
/// `(project file path, default mode is direct, defines replay_on_change, defines
/// bounded_replay_fallback)`.
pub(crate) type ProjectPolicyArgument = (String, bool, bool, bool);

pub(crate) fn pipeline_facts(pipelines: Vec<PipelineFactsArgument>) -> Vec<PipelineFacts> {
    pipelines
        .into_iter()
        .map(
            |(name, file_path, is_virtual, replay_on_change, bounded_replay_fallback, models)| {
                PipelineFacts {
                    name,
                    file_path,
                    is_virtual,
                    policy: ReplayPolicyFacts {
                        replay_on_change,
                        bounded_replay_fallback,
                    },
                    models: models
                        .into_iter()
                        .map(
                            |(name, is_table, replay_on_change, bounded_replay_fallback)| {
                                ModelFacts {
                                    name,
                                    is_table,
                                    policy: ReplayPolicyFacts {
                                        replay_on_change,
                                        bounded_replay_fallback,
                                    },
                                }
                            },
                        )
                        .collect(),
                }
            },
        )
        .collect()
}

pub(crate) fn project_policy_facts(
    project: Option<ProjectPolicyArgument>,
) -> Option<ProjectPolicyFacts> {
    project.map(
        |(file_path, default_mode_is_direct, replay_on_change, bounded_replay_fallback)| {
            ProjectPolicyFacts {
                file_path,
                default_mode_is_direct,
                policy: ReplayPolicyFacts {
                    replay_on_change,
                    bounded_replay_fallback,
                },
            }
        },
    )
}

/// Each SQL test CTE's role (`helper`, `ref_mock`, `source_mock`, `expected`, `assertion`,
/// `macro_actual`, `macro_expected`); an invalid test raises `ValueError(detail)`, the message
/// without its `SQL test '<path>' ` prefix.
#[pyfunction]
fn classify_sql_test_ctes(names: Vec<String>, macro_mode: bool) -> PyResult<Vec<&'static str>> {
    let names: Vec<&str> = names.iter().map(String::as_str).collect();
    let classify = if macro_mode {
        classify_macro_test_ctes
    } else {
        classify_model_test_ctes
    };
    classify(&names)
        .map(|roles| roles.into_iter().map(CteRole::as_str).collect())
        .map_err(PyValueError::new_err)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(infer_pipeline_sources, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_variable_values, module)?)?;
    module.add_function(wrap_pyfunction!(interpolate_config_value, module)?)?;
    module.add_function(wrap_pyfunction!(classify_sql_test_ctes, module)?)
}
