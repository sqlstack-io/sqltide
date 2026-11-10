//! Assemble one dev server application over the shared long-running state, and serve it.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyTuple};

use super::app::{DevApp, DevApplication, Services, route_table};
use super::auth::{AuthService, LoginLimiter};
use super::static_files::StaticAssets;
use super::support::attr;
use crate::handling::while_handling;
use crate::py_kwargs;

const NATIVE: &str = "sqltide._native";
const LOCALHOST_NAME: &str = "localhost";
/// Control-store URLs that name a private in-memory SQLite database.
pub(crate) const SQLITE_MEMORY_URLS: [&str; 2] = ["sqlite://", "sqlite:///:memory:"];

fn default_control_store_url(py: Python<'_>, project_dir: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(attr(py, "sqltide.auth.main.default_control_store_url", "default_control_store_url")?
        .call((), Some(&py_kwargs!(py; project_dir = project_dir)?))?
        .unbind())
}

fn disabled_settings<'py>(py: Python<'py>, project_dir: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let mode = attr(py, "sqltide.auth.types", "AuthenticationMode")?.getattr("DISABLED")?;
    attr(py, "sqltide.auth.models", "AuthSettings")?.call(
        (),
        Some(&py_kwargs!(py; mode = mode, control_store_url = default_control_store_url(py, project_dir)?)?),
    )
}

/// The overlay every state request reads: a private observation reader when one is
/// configured, else the shared client under the query lock.
fn state_snapshot<'py>(
    py: Python<'py>,
    state: &Bound<'py, PyAny>,
    warehouse: &Bound<'py, PyAny>,
    database: Option<String>,
    kafka_lag_reader: &Bound<'py, PyAny>,
    overlay_connection_factory: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let reader: Option<Py<PyAny>> = if overlay_connection_factory.is_none() {
        None
    } else {
        Some(
            attr(py, NATIVE, "OverlayReader")?
                .call(
                    (),
                    Some(&py_kwargs!(py; connection_factory = overlay_connection_factory, kafka_lag_reader = kafka_lag_reader)?),
                )?
                .unbind(),
        )
    };
    let health_reader: Py<PyAny> = attr(py, NATIVE, "WarehouseHealthReader")?.call0()?.unbind();
    let on_close: Bound<'py, PyAny> = match &reader {
        Some(reader) => reader.bind(py).getattr("close")?,
        None => py.None().into_bound(py),
    };
    // The state holds this snapshot, so the snapshot refers back to it weakly: a strong
    // reference would form a cycle the garbage collector cannot see through native objects.
    let state: Py<PyAny> = attr(py, "weakref", "ref")?.call1((state,))?.unbind();
    let warehouse: Py<PyAny> = warehouse.clone().unbind();
    let lag_reader: Py<PyAny> = kafka_lag_reader.clone().unbind();
    let build = PyCFunction::new_closure(py, None, None, move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| -> PyResult<Py<PyAny>> {
        let py: Python<'_> = args.py();
        let not_compiled = |detail: &str| -> PyErr {
            match attr(py, "sqltide.dev_server.exceptions", "ProjectNotCompiledError").and_then(|class| class.call1((detail,))) {
                Ok(value) => PyErr::from_value(value),
                Err(error) => error,
            }
        };
        let Some(database) = database.clone() else {
            return Err(not_compiled("no warehouse connection"));
        };
        let state = state.bind(py).call0()?;
        if state.is_none() {
            return Err(not_compiled("the dev server state was closed"));
        }
        let analysis = state.call_method0("current_analysis")?;
        if let Some(reader) = &reader {
            return Ok(reader
                .bind(py)
                .call_method("read", (), Some(&py_kwargs!(py; analysis = analysis, database = database.as_str())?))?
                .unbind());
        }
        let connection = warehouse.bind(py).getattr("connection")?;
        if connection.is_none() {
            return Err(not_compiled("no warehouse connection"));
        }
        let lock = state.getattr("query_lock")?;
        crate::handling::within(&lock, || {
            Ok(attr(py, NATIVE, "build_state_payload")?
                .call(
                    (),
                    Some(&py_kwargs!(py;
                        analysis = &analysis,
                        connection = &connection,
                        database = database.as_str(),
                        kafka_lag_reader = lag_reader.bind(py),
                        warehouse_health_reader = health_reader.bind(py),
                    )?),
                )?
                .unbind())
        })
    })?;
    attr(py, NATIVE, "StateSnapshot")?.call((), Some(&py_kwargs!(py; build = build, on_close = on_close)?))
}

/// Assemble one application over the shared long-running server state.
#[pyfunction]
#[pyo3(signature = (*, state, connection = None, observation_connection = None, database = None, project_dir = None, reporter = None, execution_context = None, auth_settings = None, control_store = None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn create_dev_app(
    py: Python<'_>,
    state: Bound<'_, PyAny>,
    connection: Option<Bound<'_, PyAny>>,
    observation_connection: Option<Bound<'_, PyAny>>,
    database: Option<String>,
    project_dir: Option<Bound<'_, PyAny>>,
    reporter: Option<Bound<'_, PyAny>>,
    execution_context: Option<Bound<'_, PyAny>>,
    auth_settings: Option<Bound<'_, PyAny>>,
    control_store: Option<Bound<'_, PyAny>>,
) -> PyResult<DevApplication> {
    let reporter = match reporter {
        Some(reporter) => reporter,
        None => attr(py, NATIVE, "SilentDevServerReporter")?.call0()?,
    };
    let mut context = match execution_context {
        Some(context) => context,
        None => attr(py, "sqltide.dev_server.models", "DevExecutionContext")?
            .call((), Some(&py_kwargs!(py; database = database.clone())?))?,
    };
    let held: Option<String> = context.getattr("database")?.extract()?;
    if let (Some(requested), Some(held)) = (&database, &held)
        && requested != held {
            let error = attr(py, "sqltide.dev_server.exceptions", "DevConfigurationError")?.call1((format!(
                "Dev server database does not match its retained execution context: '{requested}' != '{held}'"
            ),))?;
            return Err(PyErr::from_value(error));
        }
    if held.is_none() {
        context = attr(py, "dataclasses", "replace")?.call((&context,), Some(&py_kwargs!(py; database = database.clone())?))?;
    }
    let effective_database: Option<String> = context.getattr("database")?.extract()?;
    let project_dir = match project_dir {
        Some(project_dir) => project_dir,
        None => attr(py, "pathlib", "Path")?.call_method0("cwd")?,
    };
    let settings = match auth_settings {
        Some(settings) => settings,
        None => disabled_settings(py, &project_dir)?,
    };
    let owns_accounts: bool = control_store.is_none();
    let accounts = match control_store {
        Some(store) => store,
        None => attr(py, "sqltide.auth.classes.control_store", "ControlStore")?
            .call((), Some(&py_kwargs!(py; url = settings.getattr("control_store_url")?)?))?,
    };
    let auth: AuthService = AuthService::new(py, settings.clone().unbind(), accounts.clone().unbind())?;
    if let (Some(connection), Some(database)) = (&connection, &effective_database) {
        connection.call_method1("validate_metadata_state", (database.as_str(),))?;
    }
    let lock = state.getattr("query_lock")?;
    let warehouse = attr(py, NATIVE, "WarehouseRuntime")?.call(
        (),
        Some(&py_kwargs!(py;
            connection = connection.clone(),
            observation_connection = observation_connection,
            connection_factory = context.getattr("connection_factory")?,
            observation_connection_factory = context.getattr("observation_connection_factory")?,
            database = effective_database.clone(),
            query_lock = lock,
        )?),
    )?;
    let cancellation = attr(py, NATIVE, "BuildCancellationCoordinator")?
        .call((), Some(&py_kwargs!(py; warehouse = &warehouse, database = effective_database.clone())?))?;
    let builds = attr(py, NATIVE, "BuildProcessManager")?.call(
        (),
        Some(&py_kwargs!(py;
            reporter = &reporter,
            execution_context = &context,
            query_canceller = cancellation.getattr("cancel_query")?,
            cancellation_reporter = cancellation.getattr("record_outcome")?,
        )?),
    )?;
    let kafka_lag_reader = attr(py, NATIVE, "KafkaLagReader")?.call0()?;
    let kafka_topic_reader = attr(py, NATIVE, "KafkaTopicReader")?.call0()?;
    let presumed_failed_after_seconds = context.getattr("run_presumed_failed_after_seconds")?;
    let audit_scheduler = attr(py, NATIVE, "AuditScheduler")?.call(
        (),
        Some(&py_kwargs!(py;
            state = &state,
            warehouse = &warehouse,
            database = effective_database.clone(),
            project_dir = &project_dir,
            builds = &builds,
            presumed_failed_after_seconds = &presumed_failed_after_seconds,
        )?),
    )?;
    let sensor_scheduler = attr(py, NATIVE, "SensorScheduler")?.call(
        (),
        Some(&py_kwargs!(py; state = &state, warehouse = &warehouse, database = effective_database.clone())?),
    )?;
    let snapshot = state_snapshot(
        py,
        &state,
        &warehouse,
        effective_database.clone(),
        &kafka_lag_reader,
        &context.getattr("observation_connection_factory")?,
    )?;
    state.call_method1("attach_snapshot", (snapshot,))?;
    let accounts_url: String = accounts.getattr("url")?.extract()?;
    let plans_url: Bound<'_, PyAny> = if SQLITE_MEMORY_URLS.contains(&accounts_url.as_str()) {
        default_control_store_url(py, &project_dir)?.into_bound(py)
    } else {
        accounts.getattr("url")?
    };
    let plans = attr(py, "sqltide.executor.destruction.classes.relational_destruction_plan_store", "RelationalDestructionPlanStore")?
        .call((), Some(&py_kwargs!(py; url = plans_url)?));
    let plans = match plans {
        Ok(plans) => plans,
        Err(error) => {
            if owns_accounts
                && let Err(closing) = accounts.call_method0("close") {
                    return Err(while_handling(py, closing, &error));
                }
            return Err(error);
        }
    };
    let control_stores = attr(py, "sqltide.dev_server.models", "DevControlStores")?.call(
        (),
        Some(&py_kwargs!(py; accounts = &accounts, destruction_plans = plans, owns_accounts = owns_accounts)?),
    )?;
    let selected_target: Option<String> = context.getattr("selected_target")?.extract()?;
    let authorization = attr(py, "sqltide.dev_server.models", "OperationAuthorizationContext")?.call(
        (),
        Some(&py_kwargs!(py; store = &accounts, project_dir = &project_dir, selected_target = selected_target.clone())?),
    )?;
    let services: Services = Services {
        state: state.unbind(),
        warehouse: warehouse.unbind(),
        builds: builds.unbind(),
        audit_scheduler: audit_scheduler.unbind(),
        sensor_scheduler: sensor_scheduler.unbind(),
        kafka_lag_reader: kafka_lag_reader.unbind(),
        kafka_topic_reader: kafka_topic_reader.unbind(),
        control_stores: control_stores.unbind(),
        reporter: reporter.unbind(),
        execution_context: context.unbind(),
        authorization: authorization.unbind(),
        project_dir: project_dir.unbind(),
        database: effective_database,
        selected_target,
        presumed_failed_after_seconds: presumed_failed_after_seconds.extract()?,
    };
    Ok(DevApplication {
        app: Arc::new(DevApp {
            auth,
            limiter: LoginLimiter::default(),
            services,
            routes: route_table(),
            assets: Mutex::new(None),
            dispatches: Mutex::new(std::collections::HashSet::new()),
        }),
    })
}

/// The packaged UI build directory; it may not exist in dev checkouts.
#[pyfunction]
pub(crate) fn static_assets_root(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let package = attr(py, "pathlib", "Path")?.call1((py.import("sqltide")?.getattr("__file__")?,))?;
    package
        .call_method0("resolve")?
        .getattr("parent")?
        .call_method1("__truediv__", ("dev_server",))?
        .call_method1("__truediv__", ("static",))
}

/// Whether a built UI exists at `assets_root`.
#[pyfunction]
#[pyo3(signature = (*, assets_root))]
pub(crate) fn static_assets_present(assets_root: PathBuf) -> bool {
    StaticAssets::present(&assets_root)
}

fn is_loopback_bind(host: &str) -> bool {
    if host == LOCALHOST_NAME {
        return true;
    }
    // `ipaddress` accepts an IPv6 zone such as `::1%lo`; the zone does not change the address.
    if let Some((address, zone)) = host.split_once('%') {
        return !zone.is_empty()
            && !zone.contains('%')
            && address.parse::<std::net::Ipv6Addr>().is_ok_and(|address| {
                address.is_loopback() || address.to_ipv4_mapped().is_some_and(|mapped| mapped.is_loopback())
            });
    }
    host.parse::<std::net::IpAddr>().is_ok_and(|address| match address {
        std::net::IpAddr::V4(address) => address.is_loopback(),
        std::net::IpAddr::V6(address) => {
            address.is_loopback() || address.to_ipv4_mapped().is_some_and(|mapped| mapped.is_loopback())
        }
    })
}

fn eprint(py: Python<'_>, text: &str) -> PyResult<()> {
    let stderr = py.import("sys")?.getattr("stderr")?;
    attr(py, "builtins", "print")?.call((text,), Some(&py_kwargs!(py; file = stderr)?))?;
    Ok(())
}

/// Compile once, serve the API and packaged UI, and block until shutdown.
#[pyfunction]
#[pyo3(signature = (*, run_compile, connection, observation_connection, database, project_dir, host, port, reporter, execution_context = None, auth_settings = None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_dev_server(
    py: Python<'_>,
    run_compile: Bound<'_, PyAny>,
    connection: Option<Bound<'_, PyAny>>,
    observation_connection: Option<Bound<'_, PyAny>>,
    database: Option<String>,
    project_dir: Bound<'_, PyAny>,
    host: String,
    port: u16,
    reporter: Bound<'_, PyAny>,
    execution_context: Option<Bound<'_, PyAny>>,
    auth_settings: Option<Bound<'_, PyAny>>,
) -> PyResult<i32> {
    let settings = match auth_settings {
        Some(settings) => settings,
        None => disabled_settings(py, &project_dir)?,
    };
    let mode: String = settings.getattr("mode")?.str()?.to_string();
    if mode == "disabled" {
        eprint(py, "sqt dev: authentication disabled; all clients have administrator access.")?;
    }
    if mode == "password" && !settings.getattr("session_cookie_secure")?.is_truthy()? && !is_loopback_bind(&host) {
        eprint(py, "sqt dev: insecure password cookies may only be used on a loopback address.")?;
        return Ok(1);
    }
    let native = py.import("sqltide._native")?;
    let assets_root = native.getattr("static_assets_root")?.call0()?;
    if !native
        .getattr("static_assets_present")?
        .call((), Some(&py_kwargs!(py; assets_root = &assets_root)?))?
        .is_truthy()?
    {
        eprint(
            py,
            &format!("sqt dev: built UI not found at {}; run `make ui-build` to build and package it.", assets_root.str()?),
        )?;
        return Ok(1);
    }
    let state = attr(py, NATIVE, "DevServerState")?.call((), Some(&py_kwargs!(py; run_compile = run_compile)?))?;
    let outcome = state.call_method0("current")?;
    let application = native.getattr("create_dev_app")?.call(
        (),
        Some(&py_kwargs!(py;
            state = &state,
            connection = connection.clone(),
            observation_connection = observation_connection,
            database = database.clone(),
            project_dir = &project_dir,
            reporter = &reporter,
            execution_context = execution_context,
            auth_settings = settings,
        )?),
    )?;
    application.call_method1("mount_static_assets", (&assets_root,))?;
    reporter.call_method(
        "report_startup",
        (),
        Some(&py_kwargs!(py;
            outcome = outcome,
            project_dir = &project_dir,
            database = database,
            warehouse_connected = connection.is_some(),
            host = host.as_str(),
            port = port,
        )?),
    )?;
    // A startup or bind failure raises `SystemExit(3)`, the earlier server's exit code.
    application.call_method1("serve", (host.as_str(), port))?;
    reporter.call_method0("report_shutdown")?;
    Ok(0)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<DevApplication>()?;
    module.add_function(wrap_pyfunction!(create_dev_app, module)?)?;
    module.add_function(wrap_pyfunction!(static_assets_root, module)?)?;
    module.add_function(wrap_pyfunction!(static_assets_present, module)?)?;
    module.add_function(wrap_pyfunction!(run_dev_server, module)?)?;
    super::payloads::register(module)?;
    super::plan::register(module)?;
    super::activity::register(module)?;
    super::drift::register(module)?;
    super::state::register(module)?;
    super::runs::register(module)?;
    super::messages::register(module)?;
    super::readers::register(module)?;
    super::lifecycle::register(module)?;
    super::audits::register(module)?;
    super::checks::register(module)?;
    super::build_process::register(module)?;
    super::deployments::register(module)?;
    super::terminal::register(module)?;
    super::warehouse::register(module)?;
    super::sensor_scheduler::register(module)
}
