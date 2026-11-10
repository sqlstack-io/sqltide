//! Durable steps memoizing side-effect results per (sensor, event, key), and their stores.

use pyo3::exceptions::{PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::support::{ADAPTER_MODELS, SENSOR_EXCEPTIONS, SENSOR_MODELS, SENSOR_TYPES};
use crate::dev_server::support::attr;
use crate::py_kwargs;

type Any<'py> = Bound<'py, PyAny>;

fn step_error(py: Python<'_>, message: String) -> PyErr {
    match attr(py, SENSOR_EXCEPTIONS, "SensorStepError").and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

fn member<'py>(py: Python<'py>, class: &str, name: &str) -> PyResult<Any<'py>> {
    attr(py, SENSOR_TYPES, class)?.getattr(name)
}

/// `json.dumps(value, sort_keys=True)`, failing as a step error when `value` is not JSON.
fn encode_result(py: Python<'_>, step_key: &Any<'_>, value: &Any<'_>) -> PyResult<String> {
    let arguments: Bound<'_, PyDict> = PyDict::new(py);
    arguments.set_item("sort_keys", true)?;
    match attr(py, "json", "dumps")?.call((value,), Some(&arguments)) {
        Ok(text) => text.extract(),
        Err(error)
            if error.is_instance_of::<PyTypeError>(py)
                || error.is_instance_of::<PyValueError>(py) =>
        {
            let reason: String = error.value(py).str()?.to_string();
            let failure: PyErr = step_error(
                py,
                format!(
                    "Step '{step_key}' returned a value that is not JSON-serializable: {reason}"
                ),
            );
            failure.set_cause(py, Some(error));
            Err(failure)
        }
        Err(error) => Err(error),
    }
}

fn decode_result<'py>(
    py: Python<'py>,
    step_key: &Any<'py>,
    result_json: Any<'py>,
) -> PyResult<Any<'py>> {
    if result_json.is_none() {
        return Err(step_error(
            py,
            format!("Step '{step_key}' succeeded without a memoized result"),
        ));
    }
    attr(py, "json", "loads")?.call1((result_json,))
}

/// At-least-once and at-most-once durable steps over a persisted store.
#[pyclass(module = "sqltide._native")]
pub(crate) struct DurableStepRunner {
    store: Py<PyAny>,
}

impl DurableStepRunner {
    fn record(
        &self,
        key: &Any<'_>,
        policy: &str,
        status: &str,
        attempt: Any<'_>,
        result_json: Option<String>,
        error_message: Option<String>,
    ) -> PyResult<()> {
        let py: Python<'_> = key.py();
        self.store.bind(py).call_method(
            "record_step",
            (),
            Some(&py_kwargs!(py;
                step_key = key,
                policy = member(py, "StepPolicy", policy)?,
                status = member(py, "SensorTickStatus", status)?,
                attempt = attempt,
                result_json = result_json,
                error_message = error_message,
            )?),
        )?;
        Ok(())
    }

    fn run_at_least_once<'py>(
        &self,
        py: Python<'py>,
        key: &Any<'py>,
        function: &Any<'py>,
        existing: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        let previous: Any<'py> = if existing.is_none() {
            0_i64.into_pyobject(py)?.into_any()
        } else {
            existing.getattr("attempt")?
        };
        let attempt: Any<'py> = previous.add(1)?;
        let value: Any<'py> = function.call0()?;
        let result_json: String = encode_result(py, key, &value)?;
        self.record(
            key,
            "AT_LEAST_ONCE",
            "SUCCEEDED",
            attempt,
            Some(result_json),
            None,
        )?;
        Ok(value)
    }

    fn run_at_most_once<'py>(
        &self,
        py: Python<'py>,
        key: &Any<'py>,
        function: &Any<'py>,
        existing: &Any<'py>,
    ) -> PyResult<Any<'py>> {
        if !existing.is_none() {
            return Err(step_error(
                py,
                format!(
                    "Step '{key}' was already attempted under at-most-once policy and \
                     produced no memoized result"
                ),
            ));
        }
        let one = || -> PyResult<Any<'py>> { Ok(1_i64.into_pyobject(py)?.into_any()) };
        self.record(key, "AT_MOST_ONCE", "STARTED", one()?, None, None)?;
        let value: Any<'py> = match function.call0() {
            Ok(value) => value,
            Err(error) if error.is_instance_of::<PyException>(py) => {
                let message: String = error.value(py).str()?.to_string();
                self.record(key, "AT_MOST_ONCE", "FAILED", one()?, None, Some(message))?;
                return Err(error);
            }
            Err(error) => return Err(error),
        };
        let result_json: String = encode_result(py, key, &value)?;
        self.record(
            key,
            "AT_MOST_ONCE",
            "SUCCEEDED",
            one()?,
            Some(result_json),
            None,
        )?;
        Ok(value)
    }
}

#[pymethods]
impl DurableStepRunner {
    #[new]
    #[pyo3(signature = (*, store))]
    fn new(store: Py<PyAny>) -> Self {
        Self { store }
    }

    /// Run one durable step, memoizing its JSON-serializable return value.
    #[pyo3(signature = (key, r#fn, policy=None))]
    fn run<'py>(
        &self,
        py: Python<'py>,
        key: Any<'py>,
        r#fn: Any<'py>,
        policy: Option<Any<'py>>,
    ) -> PyResult<Any<'py>> {
        let existing: Any<'py> = self.store.bind(py).call_method(
            "read_step",
            (),
            Some(&py_kwargs!(py; step_key = &key)?),
        )?;
        if !existing.is_none()
            && existing
                .getattr("status")?
                .eq(member(py, "SensorTickStatus", "SUCCEEDED")?)?
        {
            return decode_result(py, &key, existing.getattr("result_json")?);
        }
        let at_most_once: bool = match &policy {
            Some(policy) => policy.is(&member(py, "StepPolicy", "AT_MOST_ONCE")?),
            None => false,
        };
        if at_most_once {
            self.run_at_most_once(py, &key, &r#fn, &existing)
        } else {
            self.run_at_least_once(py, &key, &r#fn, &existing)
        }
    }
}

/// Process-local step markers with the persisted store's reduction semantics.
#[pyclass(module = "sqltide._native")]
pub(crate) struct InMemoryStepStore {
    markers: Py<PyDict>,
}

#[pymethods]
impl InMemoryStepStore {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self {
            markers: PyDict::new(py).unbind(),
        }
    }

    #[pyo3(signature = (*, step_key))]
    fn read_step<'py>(&self, py: Python<'py>, step_key: Any<'py>) -> PyResult<Option<Any<'py>>> {
        self.markers.bind(py).get_item(step_key)
    }

    #[pyo3(signature = (*, step_key, policy, status, attempt, result_json, error_message))]
    #[allow(clippy::too_many_arguments)]
    fn record_step(
        &self,
        py: Python<'_>,
        step_key: Any<'_>,
        policy: Any<'_>,
        status: Any<'_>,
        attempt: Any<'_>,
        result_json: Any<'_>,
        error_message: Any<'_>,
    ) -> PyResult<()> {
        let _ = (policy, error_message);
        let markers: &Bound<'_, PyDict> = self.markers.bind(py);
        if let Some(existing) = markers.get_item(&step_key)?
            && existing
                .getattr("status")?
                .eq(member(py, "SensorTickStatus", "SUCCEEDED")?)?
        {
            return Ok(());
        }
        let marker: Any<'_> = attr(py, SENSOR_MODELS, "StepMarker")?.call(
            (),
            Some(&py_kwargs!(py;
                status = status.str()?,
                result_json = result_json,
                attempt = attempt,
            )?),
        )?;
        markers.set_item(step_key, marker)
    }
}

/// Persist and reduce step markers for one (sensor, event) through the state repository.
#[pyclass(module = "sqltide._native")]
pub(crate) struct RepositoryStepStore {
    repository: Py<PyAny>,
    sensor_name: Py<PyAny>,
    event_id: Py<PyAny>,
}

#[pymethods]
impl RepositoryStepStore {
    #[new]
    #[pyo3(signature = (*, repository, sensor_name, event_id))]
    fn new(repository: Py<PyAny>, sensor_name: Py<PyAny>, event_id: Py<PyAny>) -> Self {
        Self {
            repository,
            sensor_name,
            event_id,
        }
    }

    #[pyo3(signature = (*, step_key))]
    fn read_step<'py>(&self, py: Python<'py>, step_key: Any<'py>) -> PyResult<Any<'py>> {
        self.repository.bind(py).call_method(
            "read_step",
            (),
            Some(&py_kwargs!(py;
                sensor_name = self.sensor_name.bind(py),
                event_id = self.event_id.bind(py),
                step_key = step_key,
            )?),
        )
    }

    #[pyo3(signature = (*, step_key, policy, status, attempt, result_json, error_message))]
    #[allow(clippy::too_many_arguments)]
    fn record_step(
        &self,
        py: Python<'_>,
        step_key: Any<'_>,
        policy: Any<'_>,
        status: Any<'_>,
        attempt: Any<'_>,
        result_json: Any<'_>,
        error_message: Any<'_>,
    ) -> PyResult<()> {
        let step: Any<'_> = attr(py, ADAPTER_MODELS, "AdapterSensorStepRecord")?.call(
            (),
            Some(&py_kwargs!(py;
                sensor_name = self.sensor_name.bind(py),
                event_id = self.event_id.bind(py),
                step_key = step_key,
                policy = policy.str()?,
                status = status.str()?,
                attempt = attempt,
                result_json = result_json,
                error_message = error_message,
            )?),
        )?;
        self.repository.bind(py).call_method(
            "record_step",
            (),
            Some(&py_kwargs!(py; step = step)?),
        )?;
        Ok(())
    }
}
