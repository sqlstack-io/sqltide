//! Load authored sensors from project `sensors/` modules, validate them, and the decorators that
//! declare them.

use pyo3::exceptions::{PyAttributeError, PyException, PySyntaxError};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFrozenSet, PyList, PyTuple};

use super::dispatch::event_source_for;
use super::providers::discover_provider_classes;
use super::support::{SENSOR_EXCEPTIONS, SENSOR_MODELS, SENSOR_TYPES, text};
use crate::dev_server::support::attr;
use crate::py_kwargs;

const DIAGNOSTIC_MODELS: &str = "sqltide.diagnostics.models";
const SENSORS_DIRECTORY_NAME: &str = "sensors";
const MODULE_PREFIX: &str = "sqltide_project_sensors_";
const DEFAULT_SENSOR_TIMEOUT_SECONDS: f64 = 60.0;

type Any<'py> = Bound<'py, PyAny>;

/// `SensorError(message, location=..., related_locations=...)`, chained to `cause` if given.
fn located_error(
    py: Python<'_>,
    message: String,
    location: Option<Any<'_>>,
    related: Vec<Any<'_>>,
    cause: Option<PyErr>,
) -> PyErr {
    let built = || -> PyResult<Any<'_>> {
        attr(py, SENSOR_EXCEPTIONS, "SensorError")?.call(
            (message.clone(),),
            Some(&py_kwargs!(py;
                location = location.clone(),
                related_locations = PyTuple::new(py, related.clone())?,
            )?),
        )
    };
    match built() {
        Ok(error) => {
            let error: PyErr = PyErr::from_value(error);
            if let Some(cause) = cause {
                error.set_cause(py, Some(cause));
            }
            error
        }
        Err(error) => error,
    }
}

fn plain_error(py: Python<'_>, message: String) -> PyErr {
    located_error(py, message, None, Vec::new(), None)
}

fn source_location<'py>(
    py: Python<'py>,
    path: &Any<'py>,
    line: Any<'py>,
    column: Any<'py>,
) -> PyResult<Any<'py>> {
    attr(py, DIAGNOSTIC_MODELS, "SourceLocation")?.call(
        (),
        Some(&py_kwargs!(py; path = path, line = line, column = column)?),
    )
}

fn definition_location<'py>(sensor: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = sensor.py();
    source_location(
        py,
        &sensor.getattr("file_path")?,
        sensor.getattr("definition_line")?,
        1_i64.into_pyobject(py)?.into_any(),
    )
}

/// `value or 1`.
fn or_one<'py>(value: Any<'py>) -> PyResult<Any<'py>> {
    if value.is_truthy()? {
        return Ok(value);
    }
    Ok(1_i64.into_pyobject(value.py())?.into_any())
}

/// Public sensor module paths in stable order.
fn discover_sensor_paths<'py>(project_dir: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = project_dir.py();
    let sensors_root: Any<'py> = project_dir.div(SENSORS_DIRECTORY_NAME)?;
    if !sensors_root.call_method0("is_dir")?.is_truthy()? {
        return Ok(Vec::new());
    }
    let sorted: Any<'py> = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((sensors_root.call_method1("rglob", ("*.py",))?,))?;
    let mut paths: Vec<Any<'py>> = Vec::new();
    for path in sorted.try_iter()? {
        let path: Any<'py> = path?;
        let relative: Any<'py> = path.call_method1("relative_to", (project_dir,))?;
        let mut private: bool = path.getattr("stem")?.eq("__init__")?;
        for part in relative.getattr("parts")?.try_iter()? {
            private |= part?.call_method1("startswith", ("_",))?.is_truthy()?;
        }
        if !private {
            paths.push(path);
        }
    }
    Ok(paths)
}

/// The deepest traceback line inside `file_path`, or 1.
fn exception_line<'py>(py: Python<'py>, error: &PyErr, file_path: &Any<'py>) -> PyResult<Any<'py>> {
    let frames: Any<'py> = attr(py, "traceback", "extract_tb")?
        .call1((error.value(py).getattr("__traceback__")?,))?;
    let frames: Vec<Any<'py>> = frames.try_iter()?.collect::<PyResult<_>>()?;
    let path_class: Any<'py> = attr(py, "pathlib", "Path")?;
    for frame in frames.iter().rev() {
        if path_class
            .call1((frame.getattr("filename")?,))?
            .eq(file_path)?
        {
            return or_one(frame.getattr("lineno")?);
        }
    }
    Ok(1_i64.into_pyobject(py)?.into_any())
}

/// Import one sensor file as its own module with the project directory on `sys.path`.
fn load_sensor_module<'py>(file_path: &Any<'py>, project_dir: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = file_path.py();
    let parts: Any<'py> = file_path
        .call_method1("relative_to", (project_dir,))?
        .call_method1("with_suffix", ("",))?
        .getattr("parts")?;
    let module_name: String = format!(
        "{MODULE_PREFIX}{}",
        "_".into_pyobject(py)?
            .call_method1("join", (parts,))?
            .extract::<String>()?
    );
    let util: Bound<'py, PyModule> = py.import("importlib.util")?;
    let spec: Any<'py> = util.call_method1("spec_from_file_location", (&module_name, file_path))?;
    if spec.is_none() || spec.getattr("loader")?.is_none() {
        return Err(plain_error(
            py,
            format!("Could not load sensor file {}", text(file_path)?),
        ));
    }
    let module: Any<'py> = util.call_method1("module_from_spec", (&spec,))?;
    let sys: Bound<'py, PyModule> = py.import("sys")?;
    let old_path: Bound<'py, PyList> = PyList::new(
        py,
        sys.getattr("path")?
            .try_iter()?
            .collect::<PyResult<Vec<Any<'py>>>>()?,
    )?;
    let loaded: PyResult<()> = (|| {
        sys.getattr("path")?
            .call_method1("insert", (0, project_dir.str()?))?;
        sys.getattr("modules")?.set_item(&module_name, &module)?;
        spec.getattr("loader")?
            .call_method1("exec_module", (&module,))?;
        Ok(())
    })();
    let restored: PyResult<()> = sys.setattr("path", old_path);
    let outcome: PyResult<()> = match loaded {
        Ok(()) => Ok(()),
        Err(error) if error.is_instance_of::<PySyntaxError>(py) => {
            let value: Any<'py> = error.value(py).clone().into_any();
            let location: Any<'py> = source_location(
                py,
                file_path,
                or_one(value.getattr("lineno")?)?,
                or_one(value.getattr("offset")?)?,
            )?;
            Err(located_error(
                py,
                format!("Failed to load {}: {}", text(file_path)?, text(&value)?),
                Some(location),
                Vec::new(),
                Some(error),
            ))
        }
        Err(error) if error.is_instance(py, &attr(py, SENSOR_EXCEPTIONS, "SensorError")?) => {
            Err(error)
        }
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let line: Any<'py> = exception_line(py, &error, file_path)?;
            let location: Any<'py> =
                source_location(py, file_path, line, 1_i64.into_pyobject(py)?.into_any())?;
            Err(located_error(
                py,
                format!(
                    "Failed to load {}: {}",
                    text(file_path)?,
                    text(error.value(py).as_any())?
                ),
                Some(location),
                Vec::new(),
                Some(error),
            ))
        }
        Err(error) => Err(error),
    };
    outcome?;
    restored?;
    Ok(module)
}

/// The first paragraph of the handler's docstring, or `None`.
fn sensor_description<'py>(function: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = function.py();
    let docstring: Any<'py> = attr(py, "inspect", "getdoc")?.call1((function,))?;
    if docstring.is_none() {
        return Ok(py.None().into_bound(py));
    }
    let first: Any<'py> = docstring
        .call_method1("split", ("\n\n",))?
        .get_item(0)?
        .call_method0("strip")?;
    if first.is_truthy()? {
        Ok(first)
    } else {
        Ok(py.None().into_bound(py))
    }
}

/// The sensors a loaded module declares itself, in `inspect.getmembers` order.
fn sensors_from_module<'py>(
    module: &Any<'py>,
    file_path: &Any<'py>,
    project_dir: &Any<'py>,
) -> PyResult<Vec<Any<'py>>> {
    let py: Python<'py> = module.py();
    let source: Any<'py> =
        file_path.call_method("read_text", (), Some(&py_kwargs!(py; encoding = "utf-8")?))?;
    let event_class: Any<'py> = attr(py, SENSOR_MODELS, "EventSensorDeclaration")?;
    let polling_class: Any<'py> = attr(py, SENSOR_MODELS, "PollingSensorDeclaration")?;
    let loaded_class: Any<'py> = attr(py, SENSOR_MODELS, "LoadedSensor")?;
    let module_name: Any<'py> = module.getattr("__name__")?;
    let mut loaded: Vec<Any<'py>> = Vec::new();
    for member in attr(py, "inspect", "getmembers")?
        .call1((module,))?
        .try_iter()?
    {
        let declaration: Any<'py> = member?.get_item(1)?;
        let is_event: bool = declaration.is_instance(&event_class)?;
        if !is_event && !declaration.is_instance(&polling_class)? {
            continue;
        }
        let function: Any<'py> = declaration.getattr("function")?;
        if function.getattr("__module__")?.ne(&module_name)? {
            continue;
        }
        loaded.push(loaded_class.call(
            (),
            Some(&py_kwargs!(py;
                name = declaration.getattr("name")?,
                kind = attr(py, SENSOR_TYPES, "SensorKind")?
                    .getattr(if is_event { "EVENT" } else { "POLLING" })?,
                declaration = &declaration,
                file_path = file_path,
                relative_path = file_path.call_method1("relative_to", (project_dir,))?,
                source = &source,
                definition_line = function.getattr("__code__")?.getattr("co_firstlineno")?,
                description = sensor_description(&function)?,
                timeout_seconds = declaration.getattr("timeout_seconds")?,
            )?),
        )?);
    }
    Ok(loaded)
}

/// One deterministic registry from the project's sensor modules.
fn load_project_sensors<'py>(
    project_dir: &Any<'py>,
    sensor_paths: &[Any<'py>],
) -> PyResult<Any<'py>> {
    let py: Python<'py> = project_dir.py();
    let loaded: Bound<'py, PyDict> = PyDict::new(py);
    for file_path in sensor_paths {
        let module: Any<'py> = load_sensor_module(file_path, project_dir)?;
        for sensor in sensors_from_module(&module, file_path, project_dir)? {
            let name: Any<'py> = sensor.getattr("name")?;
            if let Some(existing) = loaded.get_item(&name)? {
                let related: Any<'py> = attr(py, DIAGNOSTIC_MODELS, "RelatedDiagnosticLocation")?
                    .call(
                    (),
                    Some(&py_kwargs!(py;
                        label = "first sensor definition",
                        location = definition_location(&existing)?,
                    )?),
                )?;
                return Err(located_error(
                    py,
                    format!(
                        "Sensor name collision for '{}' in '{}' and '{}'",
                        text(&name)?,
                        text(&sensor.getattr("file_path")?)?,
                        text(&existing.getattr("file_path")?)?
                    ),
                    Some(definition_location(&sensor)?),
                    vec![related],
                    None,
                ));
            }
            loaded.set_item(name, sensor)?;
        }
    }
    attr(py, SENSOR_MODELS, "SensorRegistry")?.call((), Some(&py_kwargs!(py; sensors = loaded)?))
}

fn validate_sensor(sensor: &Any<'_>, reserved_names: &Any<'_>) -> PyResult<()> {
    let py: Python<'_> = sensor.py();
    let location: Any<'_> = definition_location(sensor)?;
    let name: Any<'_> = sensor.getattr("name")?;
    if reserved_names.contains(&name)? {
        return Err(located_error(
            py,
            format!(
                "Sensor name '{}' collides with another project resource; sensor names are \
                 globally unique alongside pipelines, models, and audits",
                text(&name)?
            ),
            Some(location),
            Vec::new(),
            None,
        ));
    }
    let declaration: Any<'_> = sensor.getattr("declaration")?;
    if declaration.is_instance(&attr(py, SENSOR_MODELS, "EventSensorDeclaration")?)?
        && let Err(error) = event_source_for(&declaration.getattr("event_type")?)
    {
        let message: String = text(error.value(py).as_any())?;
        return Err(located_error(
            py,
            message,
            Some(location),
            Vec::new(),
            Some(error),
        ));
    }
    Ok(())
}

/// Load, validate and freeze all authored sensors and providers, or `None` when there are none.
#[pyfunction]
#[pyo3(signature = (*, project_dir, reserved_names))]
fn compile_sensors<'py>(
    project_dir: Any<'py>,
    reserved_names: Any<'py>,
) -> PyResult<Option<Any<'py>>> {
    let py: Python<'py> = project_dir.py();
    let sensor_paths: Vec<Any<'py>> = discover_sensor_paths(&project_dir)?;
    let providers: Any<'py> = discover_provider_classes(&project_dir)?;
    if sensor_paths.is_empty() && !providers.is_truthy()? {
        return Ok(None);
    }
    let registry: Any<'py> = load_project_sensors(&project_dir, &sensor_paths)?;
    for sensor in registry.call_method0("ordered")?.try_iter()? {
        validate_sensor(&sensor?, &reserved_names)?;
    }
    attr(py, SENSOR_MODELS, "CompiledSensors")?
        .call(
            (),
            Some(&py_kwargs!(py; registry = registry, providers = providers)?),
        )
        .map(Some)
}

/// `frozenset(values) if values is not None else None`.
fn frozen<'py>(values: Option<Any<'py>>) -> PyResult<Option<Bound<'py, PyFrozenSet>>> {
    values
        .filter(|values| !values.is_none())
        .map(|values| {
            PyFrozenSet::new(
                values.py(),
                values.try_iter()?.collect::<PyResult<Vec<Any<'py>>>>()?,
            )
        })
        .transpose()
}

/// `name` when given, else the handler's `__name__`; an empty name is a `SensorError`.
fn sensor_name<'py>(decorator: &str, name: &Any<'py>, function: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = function.py();
    let resolved: Any<'py> = if name.is_none() {
        match function.getattr("__name__") {
            Ok(name) => name.str()?.into_any(),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
                "".into_pyobject(py)?.into_any()
            }
            Err(error) => return Err(error),
        }
    } else {
        name.clone()
    };
    if !resolved.is_truthy()? {
        return Err(plain_error(
            py,
            format!("{decorator} name must not be empty"),
        ));
    }
    Ok(resolved)
}

fn retry_policy<'py>(py: Python<'py>, retry_policy: Option<Any<'py>>) -> PyResult<Any<'py>> {
    match retry_policy {
        Some(policy) if !policy.is_none() => Ok(policy),
        _ => attr(py, SENSOR_MODELS, "SensorRetryPolicy")?.call0(),
    }
}

fn default_status_of<'py>(py: Python<'py>, default_status: Option<Any<'py>>) -> PyResult<Any<'py>> {
    let class: Any<'py> = attr(py, SENSOR_TYPES, "DefaultSensorStatus")?;
    match default_status {
        Some(status) => class.call1((status,)),
        None => class.call1((class.getattr("STOPPED")?,)),
    }
}

fn timeout_of<'py>(py: Python<'py>, timeout_seconds: Option<Any<'py>>) -> PyResult<Any<'py>> {
    match timeout_seconds {
        Some(timeout) => Ok(timeout),
        None => Ok(DEFAULT_SENSOR_TIMEOUT_SECONDS.into_pyobject(py)?.into_any()),
    }
}

/// Declare one durable event sensor reacting to derived warehouse events.
#[pyfunction]
#[pyo3(signature = (
    *,
    on,
    name=None,
    targets=None,
    triggers=None,
    maximum_event_age_seconds=None,
    default_status=None,
    retry_policy=None,
    timeout_seconds=None,
))]
#[allow(clippy::too_many_arguments)]
fn event_sensor<'py>(
    py: Python<'py>,
    on: Any<'py>,
    name: Option<Any<'py>>,
    targets: Option<Any<'py>>,
    triggers: Option<Any<'py>>,
    maximum_event_age_seconds: Option<Any<'py>>,
    default_status: Option<Any<'py>>,
    retry_policy: Option<Any<'py>>,
    timeout_seconds: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyCFunction>> {
    let timeout_seconds: Any<'py> = timeout_of(py, timeout_seconds)?;
    if timeout_seconds.le(0)? {
        return Err(plain_error(
            py,
            "event_sensor timeout_seconds must be positive".to_owned(),
        ));
    }
    let maximum_event_age_seconds: Any<'py> =
        maximum_event_age_seconds.unwrap_or_else(|| py.None().into_bound(py));
    if !maximum_event_age_seconds.is_none() && maximum_event_age_seconds.le(0)? {
        return Err(plain_error(
            py,
            "event_sensor maximum_event_age_seconds must be positive".to_owned(),
        ));
    }
    let name: Py<PyAny> = name.map_or_else(|| py.None(), Bound::unbind);
    let on: Py<PyAny> = on.unbind();
    let targets: Option<Py<PyFrozenSet>> = frozen(targets)?.map(Bound::unbind);
    let triggers: Option<Py<PyFrozenSet>> = frozen(triggers)?.map(Bound::unbind);
    let maximum_event_age_seconds: Py<PyAny> = maximum_event_age_seconds.unbind();
    let default_status: Py<PyAny> = default_status
        .map(Bound::unbind)
        .unwrap_or_else(|| py.None());
    let default_given: bool = !default_status.is_none(py);
    let retry_policy: Py<PyAny> = retry_policy.map_or_else(|| py.None(), Bound::unbind);
    let timeout_seconds: Py<PyAny> = timeout_seconds.unbind();
    PyCFunction::new_closure(
        py,
        Some(c"decorate"),
        None,
        move |arguments: &Bound<'_, PyTuple>,
              _keywords: Option<&Bound<'_, PyDict>>|
              -> PyResult<Py<PyAny>> {
            let py: Python<'_> = arguments.py();
            let function: Any<'_> = arguments.get_item(0)?;
            let sensor_name: Any<'_> = sensor_name("event_sensor", name.bind(py), &function)?;
            attr(py, SENSOR_MODELS, "EventSensorDeclaration")?
                .call(
                    (),
                    Some(&py_kwargs!(py;
                        function = &function,
                        event_type = on.bind(py),
                        name = sensor_name,
                        targets = targets.as_ref().map(|targets| targets.bind(py)),
                        triggers = triggers.as_ref().map(|triggers| triggers.bind(py)),
                        maximum_event_age_seconds = maximum_event_age_seconds.bind(py),
                        default_status = default_status_of(
                            py,
                            default_given.then(|| default_status.bind(py).clone()),
                        )?,
                        retry_policy = self::retry_policy(py, Some(retry_policy.bind(py).clone()))?,
                        timeout_seconds = timeout_seconds.bind(py),
                    )?),
                )
                .map(Bound::unbind)
        },
    )
}

/// Declare one polling sensor evaluated on a minimum interval from tick start.
#[pyfunction]
#[pyo3(signature = (
    *,
    minimum_interval_seconds,
    name=None,
    default_status=None,
    retry_policy=None,
    timeout_seconds=None,
))]
fn polling_sensor<'py>(
    py: Python<'py>,
    minimum_interval_seconds: Any<'py>,
    name: Option<Any<'py>>,
    default_status: Option<Any<'py>>,
    retry_policy: Option<Any<'py>>,
    timeout_seconds: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyCFunction>> {
    if minimum_interval_seconds.le(0)? {
        return Err(plain_error(
            py,
            "polling_sensor minimum_interval_seconds must be positive".to_owned(),
        ));
    }
    let timeout_seconds: Any<'py> = timeout_of(py, timeout_seconds)?;
    if timeout_seconds.le(0)? {
        return Err(plain_error(
            py,
            "polling_sensor timeout_seconds must be positive".to_owned(),
        ));
    }
    let name: Py<PyAny> = name.map_or_else(|| py.None(), Bound::unbind);
    let minimum_interval_seconds: Py<PyAny> = minimum_interval_seconds.unbind();
    let default_status: Py<PyAny> = default_status
        .map(Bound::unbind)
        .unwrap_or_else(|| py.None());
    let default_given: bool = !default_status.is_none(py);
    let retry_policy: Py<PyAny> = retry_policy.map_or_else(|| py.None(), Bound::unbind);
    let timeout_seconds: Py<PyAny> = timeout_seconds.unbind();
    PyCFunction::new_closure(
        py,
        Some(c"decorate"),
        None,
        move |arguments: &Bound<'_, PyTuple>,
              _keywords: Option<&Bound<'_, PyDict>>|
              -> PyResult<Py<PyAny>> {
            let py: Python<'_> = arguments.py();
            let function: Any<'_> = arguments.get_item(0)?;
            let sensor_name: Any<'_> = sensor_name("polling_sensor", name.bind(py), &function)?;
            attr(py, SENSOR_MODELS, "PollingSensorDeclaration")?
                .call(
                    (),
                    Some(&py_kwargs!(py;
                        function = &function,
                        name = sensor_name,
                        minimum_interval_seconds = minimum_interval_seconds.bind(py),
                        default_status = default_status_of(
                            py,
                            default_given.then(|| default_status.bind(py).clone()),
                        )?,
                        retry_policy = self::retry_policy(py, Some(retry_policy.bind(py).clone()))?,
                        timeout_seconds = timeout_seconds.bind(py),
                    )?),
                )
                .map(Bound::unbind)
        },
    )
}

/// Reject a `SensorRetryPolicy` without an attempt or with a negative backoff.
#[pyfunction]
#[pyo3(signature = (*, max_attempts, backoff_seconds))]
fn validate_sensor_retry_policy(max_attempts: Any<'_>, backoff_seconds: Any<'_>) -> PyResult<()> {
    let py: Python<'_> = max_attempts.py();
    if max_attempts.lt(1)? {
        return Err(plain_error(
            py,
            "SensorRetryPolicy.max_attempts must be at least 1".to_owned(),
        ));
    }
    if backoff_seconds.lt(0)? {
        return Err(plain_error(
            py,
            "SensorRetryPolicy.backoff_seconds must not be negative".to_owned(),
        ));
    }
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(compile_sensors, module)?)?;
    module.add_function(wrap_pyfunction!(validate_sensor_retry_policy, module)?)?;
    module.add_function(wrap_pyfunction!(event_sensor, module)?)?;
    module.add_function(wrap_pyfunction!(polling_sensor, module)?)
}
