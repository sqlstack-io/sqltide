//! The single-process sensor dispatch host owned by the dev-server lifecycle.

use std::sync::{Mutex, MutexGuard, PoisonError, TryLockError};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

use super::support::attr;
use crate::handling::within;
use crate::py_kwargs;

const POLL_SECONDS: f64 = 10.0;
const MAX_BACKOFF_SECONDS: f64 = 300.0;
const RETENTION_INTERVAL_SECONDS: f64 = 3600.0;

type Any<'py> = Bound<'py, PyAny>;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn scheduler_state<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, "sqltide.dev_server.types", "SensorSchedulerState")?.getattr(name)
}

fn monotonic(py: Python<'_>) -> PyResult<f64> {
    attr(py, "time", "monotonic")?.call0()?.extract()
}

/// `max(a, b)` as Python returns it: `a` unless `b` is greater.
fn python_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// One `effective_configuration.sensors` field of the compiled target, or `default`.
fn sensors_setting<'py>(analysis: &Any<'py>, field: &str, default: Any<'py>) -> PyResult<Any<'py>> {
    let loaded_project: Any<'py> = analysis.getattr("discovered_inputs")?.getattr("loaded_project")?;
    if loaded_project.is_none() {
        return Ok(default);
    }
    let configuration: Any<'py> = loaded_project.getattr("effective_configuration")?;
    if configuration.is_none() {
        return Ok(default);
    }
    configuration.getattr("sensors")?.getattr(field)
}

/// Whether the compile has no sensors to dispatch.
fn no_sensors(analysis: &Any<'_>) -> PyResult<bool> {
    let sensors: Any<'_> = analysis.getattr("sensors")?;
    Ok(sensors.is_none() || !sensors.getattr("registry")?.getattr("sensors")?.is_truthy()?)
}

struct Health {
    state: Py<PyAny>,
    consecutive_errors: u32,
    backoff_until: f64,
    latest_error: Option<Py<PyAny>>,
    last_successful_tick: Option<Py<PyAny>>,
    last_summary: Option<Py<PyAny>>,
    retention_applied_at: f64,
}

/// Poll current compile state and dispatch running sensors one pass at a time.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct SensorScheduler {
    state: Py<PyAny>,
    warehouse: Py<PyAny>,
    database: Option<Py<PyAny>>,
    stop: Py<PyAny>,
    tick_lock: Mutex<()>,
    thread: Mutex<Option<Py<PyAny>>>,
    health: Mutex<Health>,
    dispatcher_id: Py<PyAny>,
}

impl SensorScheduler {
    fn database<'py>(&self, py: Python<'py>) -> Option<Any<'py>> {
        self.database.as_ref().map(|database| database.bind(py).clone())
    }

    fn repository_of<'py>(&self, py: Python<'py>) -> PyResult<Option<Any<'py>>> {
        let connection: Any<'py> = self.warehouse.bind(py).getattr("connection")?;
        let Some(database) = self.database(py).filter(|_| !connection.is_none()) else {
            return Ok(None);
        };
        Ok(Some(attr(py, "sqltide.sensors.classes.sensor_state_repository", "SensorStateRepository")?.call(
            (),
            Some(&py_kwargs!(py; connection = connection, database = database)?),
        )?))
    }

    fn seconds_until_attempt(&self, py: Python<'_>) -> PyResult<f64> {
        let now: f64 = monotonic(py)?;
        Ok(python_max(0.0, guard(&self.health).backoff_until - now))
    }

    fn record_success(&self, py: Python<'_>, state: &str, summary: Option<Any<'_>>) -> PyResult<()> {
        let state: Py<PyAny> = scheduler_state(py, state)?.unbind();
        let now: String = attr(py, "datetime", "datetime")?
            .call_method1("now", (attr(py, "datetime", "UTC")?,))?
            .call_method1("strftime", ("%Y-%m-%d %H:%M:%S.%f",))?
            .extract()?;
        let tick: Py<PyAny> = PyString::new(py, &now[..now.len() - 3]).into_any().unbind();
        let mut health = guard(&self.health);
        health.state = state;
        health.consecutive_errors = 0;
        health.backoff_until = 0.0;
        health.latest_error = None;
        health.last_successful_tick = Some(tick);
        if let Some(summary) = summary {
            health.last_summary = Some(summary.unbind());
        }
        Ok(())
    }

    fn record_error(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        let errors: u32 = guard(&self.health).consecutive_errors + 1;
        let backoff_seconds: f64 =
            MAX_BACKOFF_SECONDS.min(POLL_SECONDS * 2_f64.powi(i32::try_from(errors - 1).unwrap_or(i32::MAX)));
        let backoff_until: f64 = monotonic(py)? + backoff_seconds;
        let value: Any<'_> = error.value(py).clone().into_any();
        let message: String =
            format!("{}: {}", value.get_type().getattr("__name__")?.str()?, value.str()?.to_str()?);
        let backing_off: Py<PyAny> = scheduler_state(py, "BACKING_OFF")?.unbind();
        let mut health = guard(&self.health);
        health.consecutive_errors = errors;
        health.backoff_until = backoff_until;
        health.latest_error = Some(PyString::new(py, &message).into_any().unbind());
        health.state = backing_off;
        Ok(())
    }

    fn maybe_apply_retention(&self, py: Python<'_>, analysis: &Any<'_>) -> PyResult<()> {
        let retention_days: Any<'_> = sensors_setting(analysis, "tick_retention_days", 0_i64.into_pyobject(py)?.into_any())?;
        let repository: Option<Any<'_>> = self.repository_of(py)?;
        let Some(repository) = repository.filter(|_| retention_days.gt(0).unwrap_or(false)) else {
            return Ok(());
        };
        if monotonic(py)? - guard(&self.health).retention_applied_at < RETENTION_INTERVAL_SECONDS {
            return Ok(());
        }
        repository.call_method("apply_tick_retention", (), Some(&py_kwargs!(py; retention_days = retention_days)?))?;
        let applied_at: f64 = monotonic(py)?;
        guard(&self.health).retention_applied_at = applied_at;
        Ok(())
    }

    fn tick_once<'py>(slf: &Bound<'py, Self>) -> PyResult<Option<Any<'py>>> {
        let py: Python<'py> = slf.py();
        let this: &Self = slf.get();
        let repository: Option<Any<'py>> = this.repository_of(py)?;
        let Some(database) = this.database(py).filter(|_| repository.is_some()) else {
            this.record_success(py, "DISABLED", None)?;
            return Ok(None);
        };
        let _ticking = match this.tick_lock.try_lock() {
            Ok(ticking) => ticking,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(None),
        };
        within(&this.state.bind(py).getattr("query_lock")?, || {
            let analysis: Any<'py> = this.state.bind(py).call_method0("current_analysis")?;
            if !attr(py, "sqltide._native", "sensors_enabled")?.call1((&analysis,))?.is_truthy()? || no_sensors(&analysis)? {
                this.record_success(py, "DISABLED", None)?;
                return Ok(None);
            }
            let dispatcher: Any<'py> = slf.call_method("build_dispatcher", (), Some(&py_kwargs!(py; analysis = &analysis)?))?;
            if dispatcher.is_none() {
                this.record_success(py, "DISABLED", None)?;
                return Ok(None);
            }
            let summary: Any<'py> = dispatcher.call_method(
                "dispatch_once",
                (),
                Some(&py_kwargs!(py; registry = analysis.getattr("sensors")?.getattr("registry")?, target = &database)?),
            )?;
            this.maybe_apply_retention(py, &analysis)?;
            let state: &str = if summary.getattr("lease_acquired")?.is_truthy()? { "IDLE" } else { "STANDBY" };
            this.record_success(py, state, Some(summary.clone()))?;
            Ok(Some(summary))
        })
    }
}

#[pymethods]
impl SensorScheduler {
    #[new]
    #[pyo3(signature = (*, state, warehouse, database))]
    fn new(py: Python<'_>, state: Any<'_>, warehouse: Any<'_>, database: Option<Any<'_>>) -> PyResult<Self> {
        let initial: &str =
            if !warehouse.getattr("connection")?.is_none() && database.is_some() { "IDLE" } else { "DISABLED" };
        Ok(Self {
            state: state.unbind(),
            warehouse: warehouse.unbind(),
            database: database.map(Bound::unbind),
            stop: attr(py, "threading", "Event")?.call0()?.unbind(),
            tick_lock: Mutex::new(()),
            thread: Mutex::new(None),
            health: Mutex::new(Health {
                state: scheduler_state(py, initial)?.unbind(),
                consecutive_errors: 0,
                backoff_until: 0.0,
                latest_error: None,
                last_successful_tick: None,
                last_summary: None,
                retention_applied_at: 0.0,
            }),
            dispatcher_id: attr(py, "uuid", "uuid4")?.call0()?.getattr("hex")?.unbind(),
        })
    }

    /// The shared sensor state repository, if the warehouse is configured.
    #[getter]
    fn repository<'py>(&self, py: Python<'py>) -> PyResult<Option<Any<'py>>> {
        self.repository_of(py)
    }

    /// Build one dispatcher over the shared repository for the current compile.
    #[pyo3(signature = (*, analysis))]
    fn build_dispatcher<'py>(&self, py: Python<'py>, analysis: Any<'py>) -> PyResult<Option<Any<'py>>> {
        let repository: Option<Any<'py>> = self.repository_of(py)?;
        let (Some(repository), Some(database)) = (repository, self.database(py)) else {
            return Ok(None);
        };
        let target_name: Any<'py> = analysis.getattr("compiled_project")?.getattr("target_name")?;
        let event_target: Any<'py> = if target_name.is_truthy()? { target_name } else { database };
        let sensors: Any<'py> = analysis.getattr("sensors")?;
        let providers: Any<'py> = if sensors.is_none() { PyTuple::empty(py).into_any() } else { sensors.getattr("providers")? };
        let maximum_event_age_seconds: Any<'py> = sensors_setting(&analysis, "maximum_event_age_seconds", py.None().into_bound(py))?;
        Ok(Some(attr(py, "sqltide.sensors.classes.sensor_dispatcher", "SensorDispatcher")?.call(
            (),
            Some(&py_kwargs!(py;
                repository = repository,
                event_target = event_target,
                providers = providers,
                dispatcher_id = &self.dispatcher_id,
                maximum_event_age_seconds = maximum_event_age_seconds,
            )?),
        )?))
    }

    /// Start the daemon polling loop once.
    fn start(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        if guard(&slf.get().thread).is_some() {
            return Ok(());
        }
        let thread: Any<'_> = attr(py, "threading", "Thread")?.call(
            (),
            Some(&py_kwargs!(py; target = slf.getattr("_run")?, name = "sqltide-sensors", daemon = true)?),
        )?;
        *guard(&slf.get().thread) = Some(thread.clone().unbind());
        thread.call_method0("start")?;
        Ok(())
    }

    /// Initialize new event streams before producers start.
    fn initialize_event_checkpoints(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let this: &Self = slf.get();
        if this.repository_of(py)?.is_none() {
            return Ok(());
        }
        let Some(database) = this.database(py) else {
            return Ok(());
        };
        within(&this.state.bind(py).getattr("query_lock")?, || {
            let analysis: Any<'_> = this.state.bind(py).call_method0("current_analysis")?;
            if !attr(py, "sqltide._native", "sensors_enabled")?.call1((&analysis,))?.is_truthy()? || no_sensors(&analysis)? {
                return Ok(());
            }
            let dispatcher: Any<'_> = slf.call_method("build_dispatcher", (), Some(&py_kwargs!(py; analysis = &analysis)?))?;
            if dispatcher.is_none() {
                return Ok(());
            }
            dispatcher.call_method(
                "initialize_event_checkpoints",
                (),
                Some(&py_kwargs!(py; registry = analysis.getattr("sensors")?.getattr("registry")?, target = &database)?),
            )?;
            Ok(())
        })
    }

    /// Stop polling and wait briefly for the current tick.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.stop.bind(py).call_method0("set")?;
        let thread: Option<Any<'_>> = guard(&self.thread).as_ref().map(|thread| thread.bind(py).clone());
        if let Some(thread) = thread {
            thread.call_method0("join")?;
        }
        Ok(())
    }

    /// Run at most one dispatch pass, returning its summary.
    fn tick<'py>(slf: &Bound<'py, Self>) -> PyResult<Option<Any<'py>>> {
        let py: Python<'py> = slf.py();
        if slf.get().seconds_until_attempt(py)? > 0.0 {
            return Ok(None);
        }
        match Self::tick_once(slf) {
            Err(error) if error.is_instance_of::<PyException>(py) => {
                slf.get().record_error(py, &error)?;
                Err(error)
            }
            other => other,
        }
    }

    /// Return the process-local dispatcher lifecycle and backoff state.
    fn health<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let backoff_seconds: f64 = self.seconds_until_attempt(py)?;
        let next_tick_seconds: f64 = python_max(self.seconds_until_attempt(py)?, POLL_SECONDS);
        let (state, consecutive_errors, latest_error, last_successful_tick, summary) = {
            let health = guard(&self.health);
            (
                health.state.clone_ref(py),
                health.consecutive_errors,
                health.latest_error.as_ref().map(|error| error.clone_ref(py)),
                health.last_successful_tick.as_ref().map(|tick| tick.clone_ref(py)),
                health.last_summary.as_ref().map(|summary| summary.clone_ref(py)),
            )
        };
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("state", state)?;
        payload.set_item("consecutiveErrors", consecutive_errors)?;
        payload.set_item("latestError", latest_error)?;
        payload.set_item("backoffSeconds", backoff_seconds)?;
        payload.set_item("nextTickSeconds", next_tick_seconds)?;
        payload.set_item("lastSuccessfulTick", last_successful_tick)?;
        match summary {
            None => {
                payload.set_item("lastEvaluatedCount", py.None())?;
                payload.set_item("leaseHeld", py.None())?;
            }
            Some(summary) => {
                let summary: &Bound<'py, PyAny> = summary.bind(py);
                payload.set_item("lastEvaluatedCount", summary.getattr("evaluated")?)?;
                payload.set_item("leaseHeld", summary.getattr("lease_acquired")?)?;
            }
        }
        Ok(payload)
    }

    #[pyo3(name = "_run")]
    fn run(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let stop: &Bound<'_, PyAny> = slf.get().stop.bind(py);
        while !stop.call_method0("is_set")?.is_truthy()? {
            if let Err(error) = Self::tick(slf)
                && !error.is_instance_of::<PyException>(py)
            {
                return Err(error);
            }
            stop.call_method1("wait", (python_max(POLL_SECONDS, slf.get().seconds_until_attempt(py)?),))?;
        }
        Ok(())
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SensorScheduler>()
}
