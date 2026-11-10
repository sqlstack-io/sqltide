//! The dev server's held compile, its background warehouse overlay, and the compile status
//! payload polled by the UI.

use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::routes::DEV_EXCEPTIONS;
use super::support::{attr, is_instance};
use crate::handling::within;
use crate::py_kwargs;

const DEV_MODELS: &str = "sqltide.dev_server.models";
const REFRESH_SECONDS: f64 = 15.0;
const NOT_COMPILED: &str = "The project compile is failing; fix the reported error and reload";

type Any<'py> = Bound<'py, PyAny>;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn bound<'py>(py: Python<'py>, value: &Option<Py<PyAny>>) -> Option<Any<'py>> {
    value.as_ref().map(|value| value.bind(py).clone())
}

fn not_compiled(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    Ok(PyErr::from_value(
        attr(py, DEV_EXCEPTIONS, "ProjectNotCompiledError")?.call1((message,))?,
    ))
}

fn python_lock(py: Python<'_>) -> PyResult<Py<PyAny>> {
    Ok(attr(py, "threading", "Lock")?.call0()?.unbind())
}

/// Execute one injected compile callable and capture success or failure.
#[pyfunction]
#[pyo3(signature = (*, run_compile))]
pub(crate) fn build_compile_outcome<'py>(run_compile: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = run_compile.py();
    let utc: Any<'py> = attr(py, "datetime", "UTC")?;
    let compiled_at: Any<'py> = attr(py, "datetime", "datetime")?
        .call_method("now", (), Some(&py_kwargs!(py; tz = utc)?))?
        .call_method(
            "isoformat",
            (),
            Some(&py_kwargs!(py; timespec = "seconds")?),
        )?;
    let nanoseconds: Any<'py> = attr(py, "time", "monotonic_ns")?.call0()?;
    let version_key: Any<'py> = attr(py, "builtins", "format")?.call1((nanoseconds, "x"))?;
    let outcome: Any<'py> = attr(py, DEV_MODELS, "CompileOutcome")?;
    let state =
        |name: &str| attr(py, "sqltide.dev_server.types", "CompileStateKind")?.getattr(name);
    match run_compile.call0() {
        Ok(analysis) => outcome.call(
            (),
            Some(&py_kwargs!(py;
                state = state("OK")?,
                version_key = version_key,
                compiled_at = compiled_at,
                timings = analysis.getattr("timings")?,
                analysis = analysis,
            )?),
        ),
        Err(error) if error.is_instance_of::<PyException>(py) => outcome.call(
            (),
            Some(&py_kwargs!(py;
                state = state("FAILING")?,
                version_key = version_key,
                compiled_at = compiled_at,
                error = describe_compile_error(error.value(py).as_any())?,
            )?),
        ),
        Err(error) => Err(error),
    }
}

/// Reduce one compile exception to message plus source coordinates.
#[pyfunction]
#[pyo3(signature = (*, error))]
pub(crate) fn describe_compile_error<'py>(error: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = error.py();
    let info: Any<'py> = attr(py, DEV_MODELS, "CompileErrorInfo")?;
    let diagnostic: Any<'py> =
        attr(py, "builtins", "getattr")?.call1((error, "diagnostic", py.None()))?;
    if !diagnostic.is_instance(&attr(
        py,
        "sqltide.diagnostics.models",
        "CompilerDiagnostic",
    )?)? {
        return info.call((), Some(&py_kwargs!(py; message = error.str()?)?));
    }
    let location: Any<'py> = diagnostic.getattr("location")?;
    if location.is_none() {
        return info.call(
            (),
            Some(&py_kwargs!(py; message = diagnostic.getattr("message")?)?),
        );
    }
    info.call(
        (),
        Some(&py_kwargs!(py;
            message = diagnostic.getattr("message")?,
            path = location.getattr("path")?.str()?,
            line = location.getattr("line")?,
            column = location.getattr("column")?,
            end_line = location.getattr("end_line")?,
            end_column = location.getattr("end_column")?,
        )?),
    )
}

/// Build the cheap polled status payload.
#[pyfunction]
#[pyo3(signature = (*, outcome, warehouse_status))]
pub(crate) fn build_status_payload<'py>(
    outcome: &Any<'py>,
    warehouse_status: &Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = outcome.py();
    let payload: Bound<'py, PyDict> = PyDict::new(py);
    payload.set_item(
        "toolVersion",
        attr(py, "importlib.metadata", "version")?.call1(("sqltide",))?,
    )?;
    let compile: Bound<'py, PyDict> = PyDict::new(py);
    compile.set_item("state", outcome.getattr("state")?.str()?)?;
    compile.set_item("versionKey", outcome.getattr("version_key")?)?;
    compile.set_item("compiledAt", outcome.getattr("compiled_at")?)?;
    let timings: Any<'py> = outcome.getattr("timings")?;
    if timings.is_none() {
        compile.set_item("timings", py.None())?;
    } else {
        let entries: Bound<'py, PyDict> = PyDict::new(py);
        for (key, field) in [
            ("discoveryMs", "discovery_ms"),
            ("compileInputsMs", "compile_inputs_ms"),
            ("assemblyMs", "assembly_ms"),
            ("graphMs", "graph_ms"),
            ("realizationMs", "realization_ms"),
            ("rulesMs", "rules_ms"),
        ] {
            entries.set_item(key, timings.getattr(field)?)?;
        }
        compile.set_item("timings", entries)?;
    }
    let error: Any<'py> = outcome.getattr("error")?;
    if error.is_none() {
        compile.set_item("error", py.None())?;
    } else {
        compile.set_item("error", attr(py, "dataclasses", "asdict")?.call1((error,))?)?;
    }
    payload.set_item("compile", compile)?;
    payload.set_item("warehouse", warehouse_status)?;
    Ok(payload)
}

/// Hold one warehouse overlay so requests never wait on per-relation reads.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct StateSnapshot {
    build: Py<PyAny>,
    refresh_seconds: Py<PyAny>,
    on_close: Option<Py<PyAny>>,
    build_lock: Py<PyAny>,
    payload: Mutex<Option<Py<PyAny>>>,
    stop: Py<PyAny>,
    refresh_now: Py<PyAny>,
    thread: Mutex<Option<Py<PyAny>>>,
}

impl StateSnapshot {
    fn store<'py>(&self, payload: Any<'py>) -> Any<'py> {
        let previous: Option<Py<PyAny>> = guard(&self.payload).replace(payload.clone().unbind());
        drop(previous);
        payload
    }

    fn build_now<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        within(self.build_lock.bind(py), || {
            Ok(self.store(self.build.bind(py).call0()?))
        })
    }
}

#[pymethods]
impl StateSnapshot {
    #[new]
    #[pyo3(signature = (*, build, refresh_seconds = None, on_close = None))]
    fn new(
        py: Python<'_>,
        build: Any<'_>,
        refresh_seconds: Option<Any<'_>>,
        on_close: Option<Any<'_>>,
    ) -> PyResult<Self> {
        let refresh_seconds: Any<'_> = match refresh_seconds {
            Some(seconds) => seconds,
            None => REFRESH_SECONDS.into_pyobject(py)?.into_any(),
        };
        let event =
            || -> PyResult<Py<PyAny>> { Ok(attr(py, "threading", "Event")?.call0()?.unbind()) };
        Ok(Self {
            build: build.unbind(),
            refresh_seconds: refresh_seconds.unbind(),
            on_close: on_close.map(Bound::unbind),
            build_lock: python_lock(py)?,
            payload: Mutex::new(None),
            stop: event()?,
            refresh_now: event()?,
            thread: Mutex::new(None),
        })
    }

    /// Start background refreshes once; the first request may still build inline.
    fn start(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        if guard(&slf.get().thread).is_some() {
            return Ok(());
        }
        let thread: Any<'_> = attr(py, "threading", "Thread")?.call(
            (),
            Some(&py_kwargs!(py; target = slf.getattr("_run")?, name = "sqltide-state-snapshot", daemon = true)?),
        )?;
        *guard(&slf.get().thread) = Some(thread.clone().unbind());
        thread.call_method0("start")?;
        Ok(())
    }

    /// Stop refreshing, wait for the worker, then release any owned resources.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.stop.bind(py).call_method0("set")?;
        self.refresh_now.bind(py).call_method0("set")?;
        let thread: Option<Any<'_>> = bound(py, &guard(&self.thread));
        if let Some(thread) = thread {
            thread.call_method0("join")?;
        }
        if let Some(on_close) = &self.on_close {
            on_close.bind(py).call0()?;
        }
        Ok(())
    }

    /// Return the held overlay, building it inline only before the first success.
    fn current<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        if let Some(held) = bound(py, &guard(&self.payload)) {
            return Ok(held);
        }
        within(self.build_lock.bind(py), || {
            if let Some(held) = bound(py, &guard(&self.payload)) {
                return Ok(held);
            }
            Ok(self.store(self.build.bind(py).call0()?))
        })
    }

    /// Return the held overlay without triggering a warehouse read.
    fn held<'py>(&self, py: Python<'py>) -> Option<Any<'py>> {
        bound(py, &guard(&self.payload))
    }

    /// Rebuild now, replacing the held overlay.
    fn refresh<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        self.build_now(py)
    }

    /// Drop the held overlay so the next request rebuilds against new definitions.
    fn invalidate(&self) {
        let previous: Option<Py<PyAny>> = guard(&self.payload).take();
        drop(previous);
    }

    #[pyo3(name = "_run")]
    fn run(&self, py: Python<'_>) -> PyResult<()> {
        let stop: &Bound<'_, PyAny> = self.stop.bind(py);
        let refresh_now: &Bound<'_, PyAny> = self.refresh_now.bind(py);
        while !stop.call_method0("is_set")?.is_truthy()? {
            refresh_now.call_method(
                "wait",
                (),
                Some(&py_kwargs!(py; timeout = &self.refresh_seconds)?),
            )?;
            refresh_now.call_method0("clear")?;
            if stop.call_method0("is_set")?.is_truthy()? {
                return Ok(());
            }
            if let Err(error) = self.build_now(py) {
                let expected: bool =
                    is_instance(py, &error, "sqltide.adapter.exceptions", "AdapterError")?
                        || is_instance(py, &error, DEV_EXCEPTIONS, "ProjectNotCompiledError")?;
                if !expected {
                    return Err(error);
                }
            }
        }
        Ok(())
    }
}

#[derive(Default)]
struct HeldCompile {
    outcome: Option<Py<PyAny>>,
    servable_outcome: Option<Py<PyAny>>,
    authorization_analysis: Option<Py<PyAny>>,
    snapshot: Option<Py<PyAny>>,
}

/// One long-running server's source of truth between requests: the held compile plus its
/// guarding locks.
#[pyclass(frozen, weakref, module = "sqltide._native")]
pub(crate) struct DevServerState {
    run_compile: Py<PyAny>,
    compile_lock: Py<PyAny>,
    query_lock: Py<PyAny>,
    held: Mutex<HeldCompile>,
}

impl DevServerState {
    /// Hold a new outcome, retaining the last servable analysis across failed compiles.
    fn hold(&self, outcome: &Any<'_>) -> PyResult<()> {
        let analysis: Any<'_> = outcome.getattr("analysis")?;
        let mut held = guard(&self.held);
        let mut released: Vec<Option<Py<PyAny>>> =
            vec![held.outcome.replace(outcome.clone().unbind())];
        if !analysis.is_none() {
            released.push(held.authorization_analysis.replace(analysis.unbind()));
            released.push(held.servable_outcome.replace(outcome.clone().unbind()));
        }
        drop(held);
        drop(released);
        Ok(())
    }

    /// The lock every reload holds; authorization and recompilation share one hold of it.
    pub(crate) fn compile_lock<'py>(&self, py: Python<'py>) -> Any<'py> {
        self.compile_lock.bind(py).clone()
    }

    /// The analysis whose access policy authorizes the next reload.
    pub(crate) fn authorization_analysis<'py>(&self, py: Python<'py>) -> Option<Any<'py>> {
        bound(py, &guard(&self.held).authorization_analysis)
    }

    /// Recompile while the caller holds the compile lock.
    pub(crate) fn reload_locked<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        let outcome: Any<'py> = build_compile_outcome(self.run_compile.bind(py))?;
        let snapshot: Option<Any<'py>> = bound(py, &guard(&self.held).snapshot);
        if let Some(snapshot) = snapshot {
            snapshot.call_method0("invalidate")?;
        }
        self.hold(&outcome)?;
        Ok(outcome)
    }
}

#[pymethods]
impl DevServerState {
    #[new]
    #[pyo3(signature = (*, run_compile))]
    fn new(py: Python<'_>, run_compile: Any<'_>) -> PyResult<Self> {
        Ok(Self {
            run_compile: run_compile.unbind(),
            compile_lock: python_lock(py)?,
            query_lock: python_lock(py)?,
            held: Mutex::new(HeldCompile::default()),
        })
    }

    /// Return the attached warehouse overlay snapshot.
    #[getter]
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        match bound(py, &guard(&self.held).snapshot) {
            Some(snapshot) => Ok(snapshot),
            None => Err(not_compiled(
                py,
                "The warehouse overlay snapshot is not attached",
            )?),
        }
    }

    /// Attach the overlay snapshot once the warehouse services exist.
    fn attach_snapshot(&self, snapshot: Any<'_>) {
        let previous: Option<Py<PyAny>> = guard(&self.held).snapshot.replace(snapshot.unbind());
        drop(previous);
    }

    /// Serializes warehouse access; the adapter connection is not thread-safe.
    #[getter]
    fn query_lock<'py>(&self, py: Python<'py>) -> Any<'py> {
        self.query_lock.bind(py).clone()
    }

    /// Recompile synchronously; concurrent reloads run one at a time.
    fn reload<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        within(self.compile_lock.bind(py), || self.reload_locked(py))
    }

    /// Authorize and recompile atomically against one held policy snapshot.
    #[pyo3(signature = (*, guard))]
    fn reload_guarded<'py>(&self, py: Python<'py>, guard: Any<'py>) -> PyResult<Any<'py>> {
        within(self.compile_lock.bind(py), || {
            let analysis: Option<Any<'py>> =
                bound(py, &self::guard(&self.held).authorization_analysis);
            guard.call((), Some(&py_kwargs!(py; analysis = analysis)?))?;
            self.reload_locked(py)
        })
    }

    /// Return the held outcome, compiling once on first access.
    fn current<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        within(self.compile_lock.bind(py), || {
            if let Some(outcome) = bound(py, &guard(&self.held).outcome) {
                return Ok(outcome);
            }
            let outcome: Any<'py> = build_compile_outcome(self.run_compile.bind(py))?;
            self.hold(&outcome)?;
            Ok(outcome)
        })
    }

    /// Return the latest valid definitions, retaining them across failed reloads.
    fn current_analysis<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        self.current(py)?;
        let servable: Option<Any<'py>> = bound(py, &guard(&self.held).servable_outcome);
        let analysis: Option<Any<'py>> = match servable {
            Some(outcome) => {
                Some(outcome.getattr("analysis")?).filter(|analysis| !analysis.is_none())
            }
            None => None,
        };
        analysis.ok_or_else(|| not_compiled(py, NOT_COMPILED).unwrap_or_else(|error| error))
    }

    /// Return the latest successful compile outcome or raise before first success.
    fn current_servable_outcome<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        self.current(py)?;
        let servable: Option<Any<'py>> = bound(py, &guard(&self.held).servable_outcome);
        servable.ok_or_else(|| not_compiled(py, NOT_COMPILED).unwrap_or_else(|error| error))
    }
}

/// A dev server reporter that says nothing; the default for embedded use.
#[pyclass(frozen, module = "sqltide._native")]
pub(crate) struct SilentDevServerReporter;

#[pymethods]
impl SilentDevServerReporter {
    #[new]
    fn new() -> Self {
        Self
    }

    /// Ignore the startup banner.
    #[pyo3(signature = (*, outcome, project_dir, database, warehouse_connected, host, port))]
    #[allow(unused_variables)]
    fn report_startup(
        &self,
        outcome: Any<'_>,
        project_dir: Any<'_>,
        database: Any<'_>,
        warehouse_connected: Any<'_>,
        host: Any<'_>,
        port: Any<'_>,
    ) {
    }

    /// Ignore the reload outcome.
    #[pyo3(signature = (*, outcome))]
    #[allow(unused_variables)]
    fn report_reload(&self, outcome: Any<'_>) {}

    /// Ignore the activity line.
    #[pyo3(signature = (*, category, status, tone, detail))]
    #[allow(unused_variables)]
    fn report_activity(&self, category: Any<'_>, status: Any<'_>, tone: Any<'_>, detail: Any<'_>) {}

    /// Ignore the shutdown line.
    fn report_shutdown(&self) {}
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<StateSnapshot>()?;
    module.add_class::<DevServerState>()?;
    module.add_class::<SilentDevServerReporter>()?;
    module.add_function(wrap_pyfunction!(build_compile_outcome, module)?)?;
    module.add_function(wrap_pyfunction!(describe_compile_error, module)?)?;
    module.add_function(wrap_pyfunction!(build_status_payload, module)?)
}
