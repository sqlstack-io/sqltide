//! Recoverable warehouse connections owned by the dev-server lifecycle, and warehouse-side
//! cancellation of dev-server-owned build statements.

use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

use super::support::attr;
use crate::handling::{while_handling, within, within_entered};
use crate::py_kwargs;

const INITIAL_RETRY_SECONDS: f64 = 1.0;
const MAX_RETRY_SECONDS: f64 = 30.0;
const HEALTH_CHECK_SECONDS: f64 = 10.0;
const READ_CONNECTION_LIMIT: i64 = 4;
const CANCELLATION_EVENT_KIND: &str = "cancellation_completed";
const CANCELLATION_FAILED_EVENT_KIND: &str = "cancellation_failed";
const CANCELLATION_REQUESTED_EVENT_KIND: &str = "cancellation_requested";
const BUILD_CANCELLATION_FAILED_STATUS: &str = "cancellation_failed";

type Any<'py> = Bound<'py, PyAny>;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn cloned(py: Python<'_>, value: &Option<Py<PyAny>>) -> Option<Py<PyAny>> {
    value.as_ref().map(|value| value.clone_ref(py))
}

fn bound<'py>(py: Python<'py>, value: &Option<Py<PyAny>>) -> Option<Any<'py>> {
    value.as_ref().map(|value| value.bind(py).clone())
}

/// `datetime.now(tz=UTC).isoformat(timespec="seconds")`, optionally `seconds` later.
fn iso_now(py: Python<'_>, seconds: Option<f64>) -> PyResult<Py<PyAny>> {
    let mut now: Any<'_> =
        attr(py, "datetime", "datetime")?.call_method("now", (), Some(&py_kwargs!(py; tz = attr(py, "datetime", "UTC")?)?))?;
    if let Some(seconds) = seconds {
        now = now.add(attr(py, "datetime", "timedelta")?.call((), Some(&py_kwargs!(py; seconds = seconds)?))?)?;
    }
    Ok(now.call_method("isoformat", (), Some(&py_kwargs!(py; timespec = "seconds")?))?.unbind())
}

fn monotonic(py: Python<'_>) -> PyResult<f64> {
    attr(py, "time", "monotonic")?.call0()?.extract()
}

/// `str(error)`, or `f"observation connection: {error}"`.
fn error_text(py: Python<'_>, error: &PyErr, prefix: &str) -> PyResult<Py<PyAny>> {
    let text: String = error.value(py).str()?.to_str()?.to_owned();
    Ok(PyString::new(py, &format!("{prefix}{text}")).into_any().unbind())
}

#[derive(Default)]
struct Connections {
    connection: Option<Py<PyAny>>,
    observation_connection: Option<Py<PyAny>>,
    owns_connection: bool,
    owns_observation_connection: bool,
    primary_healthy: bool,
    observation_healthy: bool,
    last_error: Option<Py<PyAny>>,
    last_attempt_at: Option<Py<PyAny>>,
    next_attempt_at: Option<Py<PyAny>>,
    retry_seconds: f64,
}

/// Hold primary and observation connections, reconnecting without stopping the UI.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct WarehouseRuntime {
    connection_factory: Option<Py<PyAny>>,
    observation_connection_factory: Option<Py<PyAny>>,
    database: Option<Py<PyAny>>,
    query_lock: Py<PyAny>,
    attempt_lock: Py<PyAny>,
    read_semaphore: Py<PyAny>,
    stop: Py<PyAny>,
    retry_now: Py<PyAny>,
    thread: Mutex<Option<Py<PyAny>>>,
    held: Mutex<Connections>,
}

impl WarehouseRuntime {
    fn raw_connection(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        cloned(py, &guard(&self.held).connection)
    }

    fn raw_observation_connection(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        cloned(py, &guard(&self.held).observation_connection)
    }

    fn connections_missing(&self) -> bool {
        let held = guard(&self.held);
        (held.connection.is_none() && self.connection_factory.is_some())
            || (held.observation_connection.is_none() && self.observation_connection_factory.is_some())
    }

    fn probe_connection(&self, py: Python<'_>) -> PyResult<()> {
        let Some(connection) = self.raw_connection(py) else {
            return Ok(());
        };
        let probed: PyResult<()> = within(self.query_lock.bind(py), || {
            connection.bind(py).call_method0("capture_warehouse_timestamp").map(drop)
        });
        match probed {
            Err(error) if error.is_instance_of::<PyException>(py) => {
                let message: Py<PyAny> = error_text(py, &error, "")?;
                let attempted: Py<PyAny> = iso_now(py, None)?;
                let next: Py<PyAny> = iso_now(py, Some(HEALTH_CHECK_SECONDS))?;
                let mut held = guard(&self.held);
                held.primary_healthy = false;
                held.last_error = Some(message);
                held.last_attempt_at = Some(attempted);
                held.next_attempt_at = Some(next);
                return Ok(());
            }
            Err(error) => return Err(error),
            Ok(()) => {}
        }
        let attempted: Py<PyAny> = iso_now(py, None)?;
        {
            let mut held = guard(&self.held);
            held.primary_healthy = true;
            held.last_error = None;
            held.last_attempt_at = Some(attempted);
            held.next_attempt_at = None;
        }
        let Some(observation) = self.raw_observation_connection(py) else {
            return Ok(());
        };
        let probed: PyResult<()> = within(self.query_lock.bind(py), || {
            observation.bind(py).call_method0("capture_warehouse_timestamp").map(drop)
        });
        match probed {
            Err(error) if error.is_instance_of::<PyException>(py) => {
                let message: Py<PyAny> = error_text(py, &error, "observation connection: ")?;
                let mut held = guard(&self.held);
                held.observation_healthy = false;
                held.last_error = Some(message);
                Ok(())
            }
            Err(error) => Err(error),
            Ok(()) => {
                let mut held = guard(&self.held);
                held.observation_healthy = true;
                held.last_error = None;
                Ok(())
            }
        }
    }

    fn attempt_connections(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let this: &Self = slf.get();
        let attempted: Py<PyAny> = iso_now(py, None)?;
        {
            let mut held = guard(&this.held);
            held.last_attempt_at = Some(attempted);
            held.next_attempt_at = None;
        }
        if let Some(factory) = &this.connection_factory
            && slf.getattr("connection")?.is_none()
        {
            let mut opened: Option<Any<'_>> = None;
            let connected: PyResult<()> = (|| {
                let connection: Any<'_> = factory.bind(py).call0()?;
                opened = Some(connection.clone());
                if let Some(database) = &this.database {
                    connection.call_method1("validate_metadata_state", (database.bind(py),))?;
                }
                Ok(())
            })();
            match connected {
                Err(error) if error.is_instance_of::<PyException>(py) => {
                    if let Some(connection) = &opened
                        && let Err(closing) = connection.call_method0("close")
                    {
                        return Err(while_handling(py, closing, &error));
                    }
                    let message: Py<PyAny> = error_text(py, &error, "")?;
                    guard(&this.held).last_error = Some(message);
                    return Ok(());
                }
                Err(error) => return Err(error),
                Ok(()) => {
                    let connection: Py<PyAny> = opened.expect("connected").unbind();
                    let mut held = guard(&this.held);
                    held.connection = Some(connection);
                    held.owns_connection = true;
                    held.primary_healthy = true;
                    held.last_error = None;
                    held.retry_seconds = INITIAL_RETRY_SECONDS;
                }
            }
        }
        if let Some(factory) = &this.observation_connection_factory
            && slf.getattr("observation_connection")?.is_none()
        {
            match factory.bind(py).call0() {
                Err(error) if error.is_instance_of::<PyException>(py) => {
                    let message: Py<PyAny> = error_text(py, &error, "observation connection: ")?;
                    guard(&this.held).last_error = Some(message);
                    return Ok(());
                }
                Err(error) => return Err(error),
                Ok(observation) => {
                    let mut held = guard(&this.held);
                    held.observation_connection = Some(observation.unbind());
                    held.owns_observation_connection = true;
                    held.observation_healthy = true;
                    held.last_error = None;
                    held.retry_seconds = INITIAL_RETRY_SECONDS;
                }
            }
        }
        Ok(())
    }
}

#[pymethods]
impl WarehouseRuntime {
    #[new]
    #[pyo3(signature = (
        *,
        connection,
        observation_connection,
        connection_factory,
        observation_connection_factory,
        database,
        query_lock = None,
    ))]
    fn new(
        py: Python<'_>,
        connection: Option<Any<'_>>,
        observation_connection: Option<Any<'_>>,
        connection_factory: Option<Any<'_>>,
        observation_connection_factory: Option<Any<'_>>,
        database: Option<Any<'_>>,
        query_lock: Option<Any<'_>>,
    ) -> PyResult<Self> {
        let query_lock: Any<'_> = match query_lock.filter(|lock| lock.is_truthy().unwrap_or(true)) {
            Some(lock) => lock,
            None => attr(py, "threading", "RLock")?.call0()?,
        };
        let event = || -> PyResult<Py<PyAny>> { Ok(attr(py, "threading", "Event")?.call0()?.unbind()) };
        let held = Connections {
            primary_healthy: connection.is_some(),
            observation_healthy: observation_connection.is_some(),
            connection: connection.map(Bound::unbind),
            observation_connection: observation_connection.map(Bound::unbind),
            retry_seconds: INITIAL_RETRY_SECONDS,
            ..Connections::default()
        };
        Ok(Self {
            connection_factory: connection_factory.map(Bound::unbind),
            observation_connection_factory: observation_connection_factory.map(Bound::unbind),
            database: database.map(Bound::unbind),
            query_lock: query_lock.unbind(),
            attempt_lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
            read_semaphore: attr(py, "threading", "BoundedSemaphore")?.call1((READ_CONNECTION_LIMIT,))?.unbind(),
            stop: event()?,
            retry_now: event()?,
            thread: Mutex::new(None),
            held: Mutex::new(held),
        })
    }

    /// Return the current primary connection, if available.
    #[getter]
    fn connection(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        let held = guard(&self.held);
        if held.primary_healthy { cloned(py, &held.connection) } else { None }
    }

    /// Return the current observation connection, if available.
    #[getter]
    fn observation_connection(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        let held = guard(&self.held);
        if held.observation_healthy { cloned(py, &held.observation_connection) } else { None }
    }

    /// Start background recovery once; an existing connection remains authoritative.
    fn start(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        if guard(&slf.get().thread).is_some() {
            return Ok(());
        }
        let thread: Any<'_> = attr(py, "threading", "Thread")?.call(
            (),
            Some(&py_kwargs!(py; target = slf.getattr("_run")?, name = "sqltide-warehouse", daemon = true)?),
        )?;
        *guard(&slf.get().thread) = Some(thread.clone().unbind());
        thread.call_method0("start")?;
        Ok(())
    }

    /// Stop recovery and close every runtime-owned connection.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.stop.bind(py).call_method0("set")?;
        self.retry_now.bind(py).call_method0("set")?;
        let thread: Option<Any<'_>> = bound(py, &guard(&self.thread));
        if let Some(thread) = thread {
            thread.call_method0("join")?;
        }
        let connections = {
            let mut held = guard(&self.held);
            [
                (held.observation_connection.take(), held.owns_observation_connection),
                (held.connection.take(), held.owns_connection),
            ]
        };
        for (connection, owned) in connections {
            if let Some(connection) = connection
                && owned
            {
                connection.bind(py).call_method0("close")?;
            }
        }
        Ok(())
    }

    /// Attempt missing connections immediately, returning primary availability.
    fn connect_now(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let py: Python<'_> = slf.py();
        within(slf.get().attempt_lock.bind(py), || {
            if slf.get().raw_connection(py).is_some() {
                slf.get().probe_connection(py)
            } else {
                Self::attempt_connections(slf)
            }
        })?;
        Ok(!slf.getattr("connection")?.is_none())
    }

    /// Wake the recovery worker without blocking the caller on warehouse traffic.
    fn request_health_check(&self, py: Python<'_>) -> PyResult<()> {
        self.retry_now.bind(py).call_method0("set").map(drop)
    }

    /// Return cheap connection and recovery state for the status endpoint.
    fn status<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let held = guard(&self.held);
        let connected: bool = held.connection.is_some() && held.primary_healthy;
        let observation_degraded: bool = connected
            && self.observation_connection_factory.is_some()
            && (held.observation_connection.is_none() || !held.observation_healthy);
        let state: &str = if observation_degraded {
            "degraded"
        } else if connected {
            "connected"
        } else if self.connection_factory.is_some() {
            "retrying"
        } else {
            "unavailable"
        };
        let error: Option<Py<PyAny>> = cloned(py, &held.last_error);
        let last_attempt_at: Option<Py<PyAny>> = cloned(py, &held.last_attempt_at);
        let next_attempt_at: Option<Py<PyAny>> = cloned(py, &held.next_attempt_at);
        drop(held);
        let payload: Bound<'py, PyDict> = PyDict::new(py);
        payload.set_item("connected", connected)?;
        payload.set_item("state", state)?;
        payload.set_item("database", bound(py, &self.database))?;
        payload.set_item("error", error)?;
        payload.set_item("lastAttemptAt", last_attempt_at)?;
        payload.set_item("nextAttemptAt", next_attempt_at)?;
        Ok(payload)
    }

    /// Yield an isolated bounded read connection, falling back to the shared client.
    fn read_connection(slf: &Bound<'_, Self>) -> PyResult<ReadConnection> {
        let py: Python<'_> = slf.py();
        let this: &Self = slf.get();
        let observation_factory: Option<Any<'_>> =
            bound(py, &this.observation_connection_factory).filter(|factory| factory.is_truthy().unwrap_or(true));
        let factory: Option<Any<'_>> = match observation_factory {
            Some(factory) => Some(factory),
            None => bound(py, &this.connection_factory),
        };
        Ok(ReadConnection {
            runtime: slf.clone().into_any().unbind(),
            factory: factory.map(Bound::unbind),
            query_lock: this.query_lock.clone_ref(py),
            semaphore: this.read_semaphore.clone_ref(py),
            opened: Mutex::new(None),
        })
    }

    #[pyo3(name = "_run")]
    fn run(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let this: &Self = slf.get();
        let stop: &Bound<'_, PyAny> = this.stop.bind(py);
        let retry_now: &Bound<'_, PyAny> = this.retry_now.bind(py);
        let attempt_lock: &Bound<'_, PyAny> = this.attempt_lock.bind(py);
        while !stop.call_method0("is_set")?.is_truthy()? {
            if this.connections_missing() {
                within(attempt_lock, || Self::attempt_connections(slf))?;
            }
            if !this.connections_missing() {
                within(attempt_lock, || this.probe_connection(py))?;
                retry_now.call_method("wait", (), Some(&py_kwargs!(py; timeout = HEALTH_CHECK_SECONDS)?))?;
                retry_now.call_method0("clear")?;
                continue;
            }
            let delay: f64 = guard(&this.held).retry_seconds;
            let next: Py<PyAny> = iso_now(py, Some(delay))?;
            guard(&this.held).next_attempt_at = Some(next);
            let started: f64 = monotonic(py)?;
            retry_now.call_method("wait", (), Some(&py_kwargs!(py; timeout = delay)?))?;
            retry_now.call_method0("clear")?;
            if monotonic(py)? - started >= delay {
                guard(&this.held).retry_seconds = (delay * 2.0).min(MAX_RETRY_SECONDS);
            }
        }
        Ok(())
    }
}

/// The context manager `WarehouseRuntime.read_connection` returns.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct ReadConnection {
    runtime: Py<PyAny>,
    factory: Option<Py<PyAny>>,
    query_lock: Py<PyAny>,
    semaphore: Py<PyAny>,
    opened: Mutex<Option<Py<PyAny>>>,
}

#[pymethods]
impl ReadConnection {
    fn __enter__<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        let Some(factory) = &self.factory else {
            let lock: &Bound<'py, PyAny> = self.query_lock.bind(py);
            lock.call_method0("__enter__")?;
            let runtime: &Bound<'py, PyAny> = self.runtime.bind(py);
            let shared: PyResult<Any<'py>> = (|| {
                let observation: Any<'py> = runtime.getattr("observation_connection")?;
                if observation.is_truthy()? { Ok(observation) } else { runtime.getattr("connection") }
            })();
            return match shared {
                Ok(connection) => Ok(connection),
                Err(error) => {
                    let raised: Any<'py> = error.value(py).clone().into_any();
                    let traceback: Any<'py> = raised.getattr("__traceback__")?;
                    if let Err(exit_error) = lock.call_method1("__exit__", (raised.get_type(), &raised, traceback)) {
                        return Err(while_handling(py, exit_error, &error));
                    }
                    Err(error)
                }
            };
        };
        self.semaphore.bind(py).call_method0("acquire")?;
        match factory.bind(py).call0() {
            Ok(connection) => {
                *guard(&self.opened) = Some(connection.clone().unbind());
                Ok(connection)
            }
            Err(error) => {
                if let Err(releasing) = self.semaphore.bind(py).call_method0("release") {
                    return Err(while_handling(py, releasing, &error));
                }
                Err(error)
            }
        }
    }

    #[pyo3(signature = (*exc_info))]
    fn __exit__(&self, py: Python<'_>, exc_info: &Bound<'_, PyTuple>) -> PyResult<bool> {
        if self.factory.is_none() {
            self.query_lock.bind(py).call_method1("__exit__", exc_info.clone())?;
            return Ok(false);
        }
        let opened: Option<Py<PyAny>> = guard(&self.opened).take();
        if let Some(connection) = opened {
            // As the generator's `finally` did, a failing close skips the release.
            connection.bind(py).call_method0("close")?;
        }
        self.semaphore.bind(py).call_method0("release")?;
        Ok(false)
    }
}

/// Cancel an exact workflow query and retain the confirmed outcome.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct BuildCancellationCoordinator {
    warehouse: Py<PyAny>,
    database: Option<Py<PyAny>>,
}

fn warehouse_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    Ok(PyErr::from_value(attr(py, "sqltide.adapter.exceptions", "AdapterWarehouseError")?.call1((message,))?))
}

#[pymethods]
impl BuildCancellationCoordinator {
    #[new]
    #[pyo3(signature = (*, warehouse, database))]
    fn new(warehouse: Any<'_>, database: Option<Any<'_>>) -> Self {
        Self { warehouse: warehouse.unbind(), database: database.map(Bound::unbind) }
    }

    /// Use an isolated connection so the blocked build connection is not reused.
    fn cancel_query<'py>(&self, py: Python<'py>, query_id: Any<'py>) -> PyResult<Any<'py>> {
        let manager: Any<'py> = self.warehouse.bind(py).call_method0("read_connection")?;
        within_entered(&manager, |connection| {
            if connection.is_none() {
                return Err(warehouse_error(py, "Warehouse cancellation connection is unavailable")?);
            }
            connection.call_method("cancel_workflow_query", (), Some(&py_kwargs!(py; query_id = &query_id)?))
        })
    }

    /// Append one diagnostic event after the child process has stopped.
    fn record_outcome(&self, py: Python<'_>, outcome: Any<'_>) -> PyResult<()> {
        let Some(database) = bound(py, &self.database) else {
            return Ok(());
        };
        let manager: Any<'_> = self.warehouse.bind(py).call_method0("read_connection")?;
        within_entered(&manager, |connection| {
            if connection.is_none() {
                return Err(warehouse_error(py, "Warehouse observation connection is unavailable")?);
            }
            let invocation_id: Any<'_> = outcome.getattr("invocation_id")?;
            let invocation_literal: Any<'_> =
                invocation_id.call_method1("replace", ("\\", "\\\\"))?.call_method1("replace", ("'", "\\'"))?;
            let format: Any<'_> = attr(py, "builtins", "format")?;
            let table: Any<'_> = attr(py, "sqltide.adapter.constants", "METADATA_RUN_EVENTS_TABLE_NAME")?;
            let query: String = format!(
                "SELECT coalesce(max(sequence), 0) + 1 AS sequence FROM `{}`.`{}` WHERE invocation_id = '{}'",
                format.call1((&database, ""))?.str()?,
                format.call1((&table, ""))?.str()?,
                format.call1((&invocation_literal, ""))?.str()?,
            );
            let sequence_result: Any<'_> = connection.call_method1("query", (query,))?;
            let sequence: Any<'_> =
                attr(py, "builtins", "int")?.call1((sequence_result.getattr("rows")?.get_item(0)?.get_item(0)?.str()?,))?;
            let terminal_event_kind: &str = if outcome.getattr("status")?.eq(BUILD_CANCELLATION_FAILED_STATUS)? {
                CANCELLATION_FAILED_EVENT_KIND
            } else {
                CANCELLATION_EVENT_KIND
            };
            let shared_payload: Bound<'_, PyDict> = PyDict::new(py);
            shared_payload.set_item("queryId", outcome.getattr("query_id")?)?;
            shared_payload.set_item("requestedAt", outcome.getattr("requested_at")?)?;
            let terminal_payload: Bound<'_, PyDict> = shared_payload.copy()?;
            for (key, field) in [
                ("status", "status"),
                ("processExitCode", "process_exit_code"),
                ("warehouseSupported", "warehouse_supported"),
                ("queryFound", "query_found"),
                ("warehouseTerminationConfirmed", "warehouse_termination_confirmed"),
                ("errorMessage", "error_message"),
            ] {
                terminal_payload.set_item(key, outcome.getattr(field)?)?;
            }
            let dumps = |payload: &Bound<'_, PyDict>| -> PyResult<Py<PyAny>> {
                Ok(attr(py, "json", "dumps")?
                    .call(
                        (payload,),
                        Some(&py_kwargs!(py;
                            sort_keys = true,
                            separators = (",", ":"),
                            default = attr(py, "builtins", "str")?,
                        )?),
                    )?
                    .unbind())
            };
            let record: Any<'_> = attr(py, "sqltide.adapter.models", "AdapterRunEventRecord")?;
            let requested: Any<'_> = record.call(
                (),
                Some(&py_kwargs!(py;
                    invocation_id = &invocation_id,
                    sequence = &sequence,
                    event_kind = CANCELLATION_REQUESTED_EVENT_KIND,
                    step_id = py.None(),
                    phase = py.None(),
                    payload_json = dumps(&shared_payload)?,
                )?),
            )?;
            let terminal: Any<'_> = record.call(
                (),
                Some(&py_kwargs!(py;
                    invocation_id = &invocation_id,
                    sequence = sequence.add(1)?,
                    event_kind = terminal_event_kind,
                    step_id = py.None(),
                    phase = py.None(),
                    payload_json = dumps(&terminal_payload)?,
                )?),
            )?;
            attr(py, "sqltide.executor.observability.main._persist_run_events", "persist_run_events")?.call(
                (),
                Some(&py_kwargs!(py;
                    connection = connection,
                    database = &database,
                    events = PyTuple::new(py, [requested, terminal])?,
                )?),
            )?;
            Ok(())
        })
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<WarehouseRuntime>()?;
    module.add_class::<ReadConnection>()?;
    module.add_class::<BuildCancellationCoordinator>()
}
