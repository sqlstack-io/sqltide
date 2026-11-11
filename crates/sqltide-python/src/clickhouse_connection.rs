//! The built-in ClickHouse adapter and connection: driver calls through clickhouse-connect,
//! warehouse probes, replay frontier caching and metadata-schema guards. Python binds each core
//! to its neutral contract class, so calls on `self` dispatch through Python and subclasses can
//! override any method.

use std::sync::Mutex;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyTuple, PyType};

use crate::dev_server::support::attr;
use crate::handling::raise_from;
use crate::py_kwargs;

const NATIVE: &str = "sqltide._native";
const CONSTANTS: &str = "sqltide.adapters.clickhouse.constants";
const MODELS: &str = "sqltide.adapter.models";
const EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const DRIVER_EXCEPTIONS: &str = "clickhouse_connect.driver.exceptions";
const CONNECTION_CLASS: &str = "sqltide.adapters.clickhouse.classes.clickhouse_connection";
const QUERY_ID_SETTING: &str = "query_id";
const MAX_TABLE_SIZE_TO_DROP_SETTING: &str = "max_table_size_to_drop";
const REPLAY_FRONTIER_TTL_SECONDS: f64 = 5.0;
const REPLAY_FRONTIER_RETENTION_SECONDS: f64 = 60.0;
const MAX_REPLAY_FRONTIER_OBSERVATIONS: usize = 128;
const NODE_RESULT_REQUIRED_COLUMNS: [&str; 8] = [
    "node_name",
    "binding_key",
    "definition_fingerprint",
    "execution_fingerprint",
    "trigger",
    "scheduled_for",
    "cadence_seconds",
    "warmup_seconds",
];

type Any<'py> = Bound<'py, PyAny>;
type Kwargs<'py> = Bound<'py, PyDict>;

fn constant<'py>(py: Python<'py>, name: &str) -> PyResult<Any<'py>> {
    attr(py, CONSTANTS, name)
}

fn model<'py>(py: Python<'py>, name: &str, kwargs: &Kwargs<'py>) -> PyResult<Any<'py>> {
    attr(py, MODELS, name)?.call((), Some(kwargs))
}

fn adapter_error(py: Python<'_>, name: &str, message: String) -> PyErr {
    match attr(py, EXCEPTIONS, name).and_then(|class| class.call1((message,))) {
        Ok(error) => PyErr::from_value(error),
        Err(failure) => failure,
    }
}

/// `native_function(**kwargs)`.
fn forward<'py>(py: Python<'py>, name: &str, kwargs: &Kwargs<'py>) -> PyResult<Any<'py>> {
    attr(py, NATIVE, name)?.call((), Some(kwargs))
}

/// `native_function(argument)`.
fn forward1<'py>(py: Python<'py>, name: &str, argument: Any<'py>) -> PyResult<Any<'py>> {
    attr(py, NATIVE, name)?.call1((argument,))
}

/// Run one driver call, raising a ClickHouse or stream failure as its translated adapter error.
fn driver_call<'py>(
    py: Python<'py>,
    call: impl FnOnce() -> PyResult<Any<'py>>,
) -> PyResult<Any<'py>> {
    call().map_err(|failure| {
        let driver_failure: PyResult<bool> = (|| {
            let value: Any<'py> = failure.value(py).clone().into_any();
            Ok(
                value.is_instance(&attr(py, DRIVER_EXCEPTIONS, "ClickHouseError")?)?
                    || value.is_instance(&attr(py, DRIVER_EXCEPTIONS, "StreamFailureError")?)?,
            )
        })();
        match driver_failure {
            Ok(true) => {
                let handled: Any<'py> = failure.value(py).clone().into_any();
                match forward1(py, "translate_clickhouse_driver_error", handled.clone())
                    .and_then(|translated| raise_from(&translated, &handled, Some(&handled)))
                {
                    Ok(translated) | Err(translated) => translated,
                }
            }
            Ok(false) => failure,
            Err(inner) => inner,
        }
    })
}

fn quote_identifier(value: &str) -> String {
    format!("`{}`", value.replace('`', "``"))
}

fn quote_string(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "''"))
}

/// `int(str(value))`.
fn integer<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    attr(value.py(), "builtins", "int")?.call1((value.str()?,))
}

fn first_cell<'py>(result: &Any<'py>) -> PyResult<Option<Any<'py>>> {
    let rows: Any<'py> = result.getattr("rows")?;
    if rows.len()? == 0 {
        return Ok(None);
    }
    Ok(Some(rows.get_item(0)?.get_item(0)?))
}

fn concat<'py>(py: Python<'py>, parts: &[Any<'py>]) -> PyResult<Bound<'py, PyTuple>> {
    let mut items: Vec<Any<'py>> = Vec::new();
    for part in parts {
        for item in part.try_iter()? {
            items.push(item?);
        }
    }
    PyTuple::new(py, items)
}

/// Refuse to build a subclass that leaves abstract contract methods unimplemented, as
/// `object.__new__` does for an ordinary ABC.
fn reject_abstract(cls: &Bound<'_, PyType>) -> PyResult<()> {
    let Ok(abstract_methods) = cls.getattr("__abstractmethods__") else {
        return Ok(());
    };
    let mut names: Vec<String> = abstract_methods
        .try_iter()?
        .map(|name| name?.extract())
        .collect::<PyResult<_>>()?;
    if names.is_empty() {
        return Ok(());
    }
    names.sort();
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "Can't instantiate abstract class {} without an implementation for abstract method{} '{}'",
        cls.name()?,
        if names.len() == 1 { "" } else { "s" },
        names.join("', '")
    )))
}

/// A neutral adapter connection backed by the ClickHouse driver.
#[pyclass(module = "sqltide._native", frozen, subclass)]
pub(crate) struct ClickHouseConnectionCore {
    raw_client: Mutex<Option<Py<PyAny>>>,
    replay_frontier_lock: Py<PyAny>,
    replay_frontier_observations: Mutex<Py<PyDict>>,
}

impl ClickHouseConnectionCore {
    fn raw_client<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        let client: Option<Py<PyAny>> = self
            .raw_client
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("connection state is poisoned"))?
            .as_ref()
            .map(|client| client.clone_ref(py));
        client
            .map(|client| client.into_bound(py))
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("_raw_client"))
    }

    fn observations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        Ok(self
            .replay_frontier_observations
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("connection state is poisoned"))?
            .bind(py)
            .clone())
    }

    fn settings<'py>(py: Python<'py>, query_id: &Any<'py>) -> PyResult<Option<Kwargs<'py>>> {
        if query_id.is_none() {
            return Ok(None);
        }
        let settings: Kwargs<'py> = PyDict::new(py);
        settings.set_item(QUERY_ID_SETTING, query_id)?;
        Ok(Some(settings))
    }

    fn validate(slf: &Bound<'_, Self>, database: &Any<'_>) -> PyResult<()> {
        slf.call_method1("validate_metadata_state", (database,))?;
        Ok(())
    }

    fn relation_drop_size_setting<'py>(slf: &Bound<'py, Self>, column: &str) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let result: Any<'py> = slf.call_method1(
            "query",
            (format!(
                "SELECT {column} FROM system.settings WHERE name = '{MAX_TABLE_SIZE_TO_DROP_SETTING}'"
            ),),
        )?;
        if result.getattr("rows")?.len()? != 1 {
            return Err(adapter_error(
                py,
                "AdapterResultError",
                format!("Could not determine ClickHouse {MAX_TABLE_SIZE_TO_DROP_SETTING}"),
            ));
        }
        let value: Any<'py> =
            integer(&first_cell(&result)?.unwrap_or_else(|| py.None().into_bound(py)))?;
        Ok(if value.eq(0)? {
            py.None().into_bound(py)
        } else {
            value
        })
    }
}

#[pymethods]
impl ClickHouseConnectionCore {
    #[new]
    #[classmethod]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn new(
        cls: &Bound<'_, PyType>,
        _args: &Bound<'_, PyTuple>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let py: Python<'_> = cls.py();
        reject_abstract(cls)?;
        Ok(Self {
            raw_client: Mutex::new(None),
            replay_frontier_lock: attr(py, "threading", "Lock")?.call0()?.unbind(),
            replay_frontier_observations: Mutex::new(PyDict::new(py).unbind()),
        })
    }

    fn __init__(&self, raw_client: Bound<'_, PyAny>) -> PyResult<()> {
        *self.raw_client.lock().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("connection state is poisoned")
        })? = Some(raw_client.unbind());
        Ok(())
    }

    /// Return the built-in ClickHouse adapter identity.
    #[getter]
    fn adapter_identity<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        model(
            py,
            "AdapterIdentity",
            &py_kwargs!(py; name = constant(py, "CLICKHOUSE_ADAPTER_NAME")?)?,
        )
    }

    /// Return capabilities implemented by the ClickHouse adapter.
    #[getter]
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        model(
            py,
            "AdapterCapabilities",
            &py_kwargs!(py;
                virtual_environments = constant(py, "CLICKHOUSE_VIRTUAL_ENVIRONMENTS_SUPPORTED")?,
                managed_source_kinds = constant(py, "CLICKHOUSE_MANAGED_SOURCE_KINDS")?,
                replay_boundary_modes = constant(py, "CLICKHOUSE_REPLAY_BOUNDARY_MODES")?,
                history_prefix_seed = constant(py, "CLICKHOUSE_HISTORY_PREFIX_SEED_SUPPORTED")?,
                stable_logical_bindings = constant(py, "CLICKHOUSE_STABLE_LOGICAL_BINDINGS_SUPPORTED")?,
                per_relation_atomic_replace = constant(py, "CLICKHOUSE_PER_RELATION_ATOMIC_REPLACE")?,
                graph_atomic_publish = constant(py, "CLICKHOUSE_GRAPH_ATOMIC_PUBLISH")?,
                set_difference_comparison = constant(py, "CLICKHOUSE_SET_DIFFERENCE_COMPARISON_SUPPORTED")?,
                direct_rebuild = constant(py, "CLICKHOUSE_DIRECT_REBUILD_SUPPORTED")?,
            )?,
        )
    }

    /// Load a neutral catalog snapshot from ClickHouse system tables.
    fn load_catalog<'py>(slf: &Bound<'py, Self>, database: Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_catalog",
            &py_kwargs!(py; connection = slf, adapter_identity = slf.getattr("adapter_identity")?, database = database)?,
        )
    }

    /// Return ClickHouse's server/session table DROP safety limit.
    fn load_relation_drop_size_limit<'py>(slf: &Bound<'py, Self>) -> PyResult<Any<'py>> {
        Self::relation_drop_size_setting(slf, "value")
    }

    /// Return ClickHouse's configured default table DROP safety limit.
    fn load_relation_drop_size_server_default<'py>(slf: &Bound<'py, Self>) -> PyResult<Any<'py>> {
        Self::relation_drop_size_setting(slf, "default")
    }

    /// Return database-qualified unmanaged dependants across ClickHouse.
    #[pyo3(signature = (*, database, relation_names))]
    fn load_external_dependants<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        relation_names: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_external_dependants",
            &py_kwargs!(py; connection = slf, database = database, relation_names = relation_names)?,
        )
    }

    /// Report ClickHouse refreshable view state so stale relations stay visible.
    fn load_refresh_states<'py>(slf: &Bound<'py, Self>, database: Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_refresh_states",
            &py_kwargs!(py; connection = slf, database = database)?,
        )
    }

    /// Return whether any partition holds more rows than its offset span.
    #[pyo3(signature = (*, database, relation, partition_column, offset_column))]
    fn replay_anchor_has_repeated_offsets(
        slf: &Bound<'_, Self>,
        database: &str,
        relation: &str,
        partition_column: &str,
        offset_column: &str,
    ) -> PyResult<bool> {
        let partition: String = quote_identifier(partition_column);
        let offset: String = quote_identifier(offset_column);
        let statement: String = format!(
            "SELECT 1 FROM (SELECT count() AS row_count, max({offset}) - min({offset}) + 1 AS \
             offset_span FROM {}.{} GROUP BY {partition}) WHERE row_count > offset_span LIMIT 1",
            quote_identifier(database),
            quote_identifier(relation)
        );
        slf.call_method1("query", (statement,))?
            .getattr("rows")?
            .is_truthy()
    }

    /// Read bounded ClickHouse system-table diagnostics.
    #[pyo3(signature = (*, database, managed_source_names = None, include_dead_letters = false))]
    fn load_warehouse_health<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        managed_source_names: Option<Any<'py>>,
        include_dead_letters: bool,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let names: Any<'py> = match managed_source_names {
            Some(names) => names,
            None => PyTuple::empty(py).into_any(),
        };
        forward(
            py,
            "load_clickhouse_warehouse_health",
            &py_kwargs!(py;
                connection = slf,
                database = database,
                managed_source_names = names,
                include_dead_letters = include_dead_letters,
            )?,
        )
    }

    /// Return available ClickHouse columns for one framework metadata table.
    #[pyo3(signature = (*, database, table))]
    fn metadata_columns<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        table: Any<'py>,
    ) -> PyResult<Bound<'py, PyFrozenSet>> {
        let py: Python<'py> = slf.py();
        let result: Any<'py> = slf.call_method1(
            "query",
            (format!(
                "SELECT name FROM system.columns WHERE database = '{}' AND table = '{}'",
                database.str()?,
                table.str()?
            ),),
        )?;
        let mut names: Vec<Any<'py>> = Vec::new();
        for row in result.getattr("rows")?.try_iter()? {
            names.push(row?.get_item(0)?.str()?.into_any());
        }
        PyFrozenSet::new(py, names)
    }

    /// Inspect ClickHouse stable bindings and deployment-specific tables.
    fn inspect_managed_table_state<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "build_inspected_managed_table_state",
            &py_kwargs!(py; client = slf, database = database)?,
        )
    }

    /// Execute a ClickHouse query and normalize the returned rows.
    fn query<'py>(slf: &Bound<'py, Self>, statement: Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        slf.call_method(
            "_query",
            (),
            Some(&py_kwargs!(py; statement = statement, query_id = py.None())?),
        )
    }

    /// Execute a workflow query with a ClickHouse query ID.
    #[pyo3(signature = (*, statement, query_id))]
    fn execute_workflow_query<'py>(
        slf: &Bound<'py, Self>,
        statement: Any<'py>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        slf.call_method(
            "_query",
            (),
            Some(&py_kwargs!(py; statement = statement, query_id = query_id)?),
        )
    }

    #[pyo3(signature = (*, statement, query_id))]
    fn _query<'py>(
        &self,
        py: Python<'py>,
        statement: Any<'py>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let client: Any<'py> = self.raw_client(py)?;
        let kwargs: Kwargs<'py> = py_kwargs!(py; query = statement)?;
        if let Some(settings) = Self::settings(py, &query_id)? {
            kwargs.set_item("settings", settings)?;
        }
        let raw: Any<'py> = driver_call(py, || client.call_method("query", (), Some(&kwargs)))?;
        let column_names: Bound<'py, PyTuple> = PyTuple::new(
            py,
            raw.getattr("column_names")?
                .try_iter()?
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let mut rows: Vec<Bound<'py, PyTuple>> = Vec::new();
        for row in raw.getattr("result_rows")?.try_iter()? {
            rows.push(PyTuple::new(
                py,
                row?.try_iter()?.collect::<PyResult<Vec<_>>>()?,
            )?);
        }
        model(
            py,
            "AdapterQueryResult",
            &py_kwargs!(py; column_names = column_names, rows = PyTuple::new(py, rows)?)?,
        )
    }

    /// Execute exact workflow SQL and preserve ClickHouse mutation evidence.
    fn execute_workflow_sql<'py>(
        slf: &Bound<'py, Self>,
        statement: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        slf.call_method(
            "_execute_workflow_sql",
            (),
            Some(&py_kwargs!(py; statement = statement, query_id = py.None())?),
        )
    }

    /// Execute workflow SQL with a ClickHouse query ID.
    #[pyo3(signature = (*, statement, query_id))]
    fn execute_workflow_mutation<'py>(
        slf: &Bound<'py, Self>,
        statement: Any<'py>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        slf.call_method(
            "_execute_workflow_sql",
            (),
            Some(&py_kwargs!(py; statement = statement, query_id = query_id)?),
        )
    }

    #[pyo3(signature = (*, statement, query_id))]
    fn _execute_workflow_sql<'py>(
        &self,
        py: Python<'py>,
        statement: Any<'py>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let client: Any<'py> = self.raw_client(py)?;
        let kwargs: Kwargs<'py> = py_kwargs!(py; cmd = statement)?;
        if let Some(settings) = Self::settings(py, &query_id)? {
            kwargs.set_item("settings", settings)?;
        }
        let summary: Any<'py> =
            driver_call(py, || client.call_method("command", (), Some(&kwargs)))?;
        let summary_class: Any<'py> =
            attr(py, "clickhouse_connect.driver.summary", "QuerySummary")?;
        if !summary.is_instance(&summary_class)?
            || !summary
                .getattr("summary")?
                .contains(constant(py, "CLICKHOUSE_WRITTEN_ROWS_SUMMARY_KEY")?)?
        {
            return model(py, "AdapterMutationResult", &PyDict::new(py));
        }
        model(
            py,
            "AdapterMutationResult",
            &py_kwargs!(py; written_rows = summary.getattr("written_rows")?)?,
        )
    }

    /// Read one active query snapshot from ClickHouse system.processes.
    #[pyo3(signature = (*, query_id))]
    fn load_statement_progress<'py>(
        slf: &Bound<'py, Self>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_statement_progress",
            &py_kwargs!(py; connection = slf, query_id = query_id)?,
        )
    }

    /// Kill one exact ClickHouse query and confirm it leaves system.processes.
    #[pyo3(signature = (*, query_id))]
    fn cancel_workflow_query<'py>(
        slf: &Bound<'py, Self>,
        query_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "cancel_clickhouse_workflow_query",
            &py_kwargs!(py; connection = slf, query_id = query_id)?,
        )
    }

    /// Observe the greatest committed target offset for each captured partition.
    #[pyo3(signature = (*, query_id, request))]
    fn load_replay_offset_frontiers<'py>(
        slf: &Bound<'py, Self>,
        query_id: Any<'py>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let this: &Self = slf.get();
        let key: Bound<'py, PyTuple> = PyTuple::new(py, [&query_id, &request])?;
        let now: f64 = attr(py, "time", "monotonic")?.call0()?.extract()?;
        let lock: &Any<'py> = this.replay_frontier_lock.bind(py);
        lock.call_method0("acquire")?;
        let observed: PyResult<Any<'py>> = (|| {
            let observations: Bound<'py, PyDict> = this.observations(py)?;
            let cached: Option<Any<'py>> = observations.get_item(&key)?;
            if let Some(cached) = &cached {
                let at: f64 = cached.get_item(0)?.extract()?;
                if now - at < REPLAY_FRONTIER_TTL_SECONDS {
                    return cached.get_item(1);
                }
            }
            let previous: Any<'py> = match &cached {
                Some(cached) => cached.get_item(1)?,
                None => PyTuple::empty(py).into_any(),
            };
            let frontiers: Any<'py> = forward(
                py,
                "observe_clickhouse_replay_offset_frontiers",
                &py_kwargs!(py; connection = slf, request = &request, previous = previous)?,
            )?;
            let retained: Bound<'py, PyDict> = PyDict::new(py);
            for (observation_key, observation) in this.observations(py)?.iter() {
                let at: f64 = observation.get_item(0)?.extract()?;
                if now - at <= REPLAY_FRONTIER_RETENTION_SECONDS {
                    retained.set_item(observation_key, observation)?;
                }
            }
            retained.set_item(
                &key,
                PyTuple::new(py, [now.into_pyobject(py)?.into_any(), frontiers.clone()])?,
            )?;
            let mut items: Vec<(f64, Any<'py>, Any<'py>)> = Vec::new();
            for (observation_key, observation) in retained.iter() {
                items.push((
                    observation.get_item(0)?.extract()?,
                    observation_key,
                    observation,
                ));
            }
            items.sort_by(|left, right| left.0.total_cmp(&right.0));
            let kept: Bound<'py, PyDict> = PyDict::new(py);
            let skip: usize = items.len().saturating_sub(MAX_REPLAY_FRONTIER_OBSERVATIONS);
            for (_, observation_key, observation) in items.into_iter().skip(skip) {
                kept.set_item(observation_key, observation)?;
            }
            *this.replay_frontier_observations.lock().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("connection state is poisoned")
            })? = kept.unbind();
            Ok(frontiers)
        })();
        lock.call_method0("release")?;
        observed
    }

    /// Capture ClickHouse server time as an exact UTC DateTime64(3) string.
    fn capture_warehouse_timestamp<'py>(slf: &Bound<'py, Self>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        let result: Any<'py> = slf.call_method1(
            "query",
            ("SELECT toString(now64(3, 'UTC'), 'UTC') AS warehouse_timestamp",),
        )?;
        match first_cell(&result)? {
            Some(value) => Ok(value.str()?.into_any()),
            None => Err(adapter_error(
                py,
                "AdapterResultError",
                "ClickHouse returned no warehouse timestamp".to_owned(),
            )),
        }
    }

    /// Acquire one target-wide lock using ClickHouse's atomic DDL namespace.
    #[pyo3(signature = (*, database, owner_id))]
    fn acquire_target_mutation_lock<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        owner_id: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "acquire_clickhouse_target_mutation_lock",
            &py_kwargs!(py; connection = slf, database = database, owner_id = owner_id)?,
        )
    }

    /// Atomically exchange and verify the exact lock generation before release.
    fn release_target_mutation_lock(slf: &Bound<'_, Self>, lock: Any<'_>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        forward(
            py,
            "release_clickhouse_target_mutation_lock",
            &py_kwargs!(py; connection = slf, lock = lock)?,
        )?;
        Ok(())
    }

    /// Render exact ClickHouse database creation SQL.
    fn render_ensure_database<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward1(py, "render_clickhouse_ensure_database", database)
    }

    /// Return whether ClickHouse currently exposes the database namespace.
    fn database_exists(slf: &Bound<'_, Self>, database: &str) -> PyResult<bool> {
        let result: Any<'_> = slf.call_method1(
            "query",
            (format!(
                "SELECT count() FROM system.databases WHERE name = {}",
                quote_string(database)
            ),),
        )?;
        match first_cell(&result)? {
            Some(value) => integer(&value)?.eq(1),
            None => Ok(false),
        }
    }

    /// Render one neutral resource request as ClickHouse SQL.
    #[pyo3(signature = (*, resource, database, if_not_exists = false))]
    fn render_resource<'py>(
        &self,
        py: Python<'py>,
        resource: Any<'py>,
        database: Any<'py>,
        if_not_exists: bool,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_resource",
            &py_kwargs!(py; resource = resource, database = database, if_not_exists = if_not_exists)?,
        )
    }

    /// Rename StreamBuild metadata tables, tolerating a concurrent identical rename.
    fn rename_streambuild_metadata(slf: &Bound<'_, Self>, database: Any<'_>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        forward(
            py,
            "rename_clickhouse_streambuild_metadata",
            &py_kwargs!(py; connection = slf, database = database)?,
        )?;
        Ok(())
    }

    /// Render the exact idempotent ClickHouse metadata migration.
    fn render_migrate_metadata_state<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        Self::validate(slf, &database)?;
        forward1(
            slf.py(),
            "render_clickhouse_metadata_migration_workflow",
            database,
        )
    }

    /// Render exact ClickHouse metadata persistence SQL.
    #[pyo3(signature = (*, database, state))]
    fn render_persist_metadata_state<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        state: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        Self::validate(slf, &database)?;
        forward(
            py,
            "render_clickhouse_metadata_state",
            &py_kwargs!(py; database = database, state = state)?,
        )
    }

    /// Render terminal UI observations without exposing them to lifecycle readers.
    #[pyo3(signature = (*, database, invocation, node_results))]
    fn render_terminal_observations<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        invocation: Any<'py>,
        node_results: Any<'py>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let py: Python<'py> = slf.py();
        Self::validate(slf, &database)?;
        let empty = || PyTuple::empty(py);
        let state: Any<'py> = model(
            py,
            "AdapterMetadataState",
            &py_kwargs!(py;
                object_states = empty(),
                deployments = empty(),
                deployment_watermarks = empty(),
                publish_events = empty(),
                invocations = PyTuple::new(py, [invocation])?,
                node_results = node_results,
            )?,
        )?;
        concat(
            py,
            &[
                forward1(
                    py,
                    "render_clickhouse_metadata_migration_workflow",
                    database.clone(),
                )?,
                forward(
                    py,
                    "render_clickhouse_metadata_state",
                    &py_kwargs!(py; database = database, state = state)?,
                )?,
            ],
        )
    }

    /// Render current, stale, and never-run Quality UI states.
    #[pyo3(signature = (*, database, project_identity, target_identity, nodes))]
    fn render_latest_node_status_query<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        project_identity: Any<'py>,
        target_identity: Any<'py>,
        nodes: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        Self::validate(slf, &database)?;
        forward(
            py,
            "render_clickhouse_latest_node_status_query",
            &py_kwargs!(py;
                database = database,
                project_identity = project_identity,
                target_identity = target_identity,
                nodes = nodes,
            )?,
        )
    }

    /// Reject the intentionally incompatible pre-0.11 quality result schema.
    fn validate_metadata_state(slf: &Bound<'_, Self>, database: Any<'_>) -> PyResult<()> {
        let py: Python<'_> = slf.py();
        let table: Any<'_> = attr(
            py,
            "sqltide.adapter.constants",
            "METADATA_NODE_RESULTS_TABLE_NAME",
        )?;
        let columns: Any<'_> = slf.call_method(
            "metadata_columns",
            (),
            Some(&py_kwargs!(py; database = &database, table = &table)?),
        )?;
        if !columns.is_truthy()?
            || PyFrozenSet::new(py, NODE_RESULT_REQUIRED_COLUMNS)?
                .call_method1("issubset", (&columns,))?
                .is_truthy()?
        {
            return Ok(());
        }
        Err(adapter_error(
            py,
            "AdapterWarehouseError",
            format!(
                "{}.{} uses an incompatible pre-0.11 schema; drop this pre-production metadata \
                 table and retry so SQLTide can recreate it",
                database.str()?,
                table.str()?
            ),
        ))
    }

    /// Render incremental run-event inserts for the live run timeline.
    #[pyo3(signature = (*, database, events, include_migration = false))]
    fn render_run_events<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
        events: Any<'py>,
        include_migration: bool,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_run_event_inserts",
            &py_kwargs!(py; database = database, events = events, include_migration = include_migration)?,
        )
    }

    /// Render the durable SQL statement set for one run.
    #[pyo3(signature = (*, database, statements, include_migration = false))]
    fn render_run_statements<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
        statements: Any<'py>,
        include_migration: bool,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_run_statement_inserts",
            &py_kwargs!(py; database = database, statements = statements, include_migration = include_migration)?,
        )
    }

    /// Render incremental sensor-state inserts for durable automation.
    #[pyo3(signature = (*, database, state, include_migration = false))]
    fn render_sensor_state<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
        state: Any<'py>,
        include_migration: bool,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_sensor_state_inserts",
            &py_kwargs!(py; database = database, state = state, include_migration = include_migration)?,
        )
    }

    /// Render bounded ClickHouse deletes for aged sensor ticks and steps.
    #[pyo3(signature = (*, database, retention_days))]
    fn render_sensor_retention_cleanup<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
        retention_days: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_sensor_retention_cleanup",
            &py_kwargs!(py; database = database, retention_days = retention_days)?,
        )
    }

    /// Load optional logical SQL baselines from ClickHouse metadata.
    #[pyo3(signature = (*, database, logical_model_identities))]
    fn load_direct_fingerprints<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        logical_model_identities: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_direct_fingerprints",
            &py_kwargs!(py; connection = slf, database = database, logical_model_identities = logical_model_identities)?,
        )
    }

    /// Render terminal best-effort logical direct SQL baselines.
    #[pyo3(signature = (*, database, fingerprints))]
    fn render_direct_fingerprint_observations<'py>(
        &self,
        py: Python<'py>,
        database: Any<'py>,
        fingerprints: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_direct_fingerprint_observations",
            &py_kwargs!(py; database = database, fingerprints = fingerprints)?,
        )
    }

    /// Load complete manifest history for one project target.
    #[pyo3(signature = (*, database, project_identity, target_name, target_database))]
    fn load_manifests<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        project_identity: Any<'py>,
        target_name: Any<'py>,
        target_database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_manifests",
            &py_kwargs!(py;
                connection = slf,
                database = database,
                project_identity = project_identity,
                target_name = target_name,
                target_database = target_database,
            )?,
        )
    }

    /// Render one complete manifest append.
    #[pyo3(signature = (*, database, manifest))]
    fn render_manifest_publication<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
        manifest: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        Self::validate(slf, &database)?;
        forward(
            py,
            "render_clickhouse_manifest_publication",
            &py_kwargs!(py; database = database, manifest = manifest)?,
        )
    }

    /// Compare a ClickHouse relation with its compiled desired resource.
    #[pyo3(signature = (*, resource, relation, database))]
    fn catalog_resource_matches<'py>(
        &self,
        py: Python<'py>,
        resource: Any<'py>,
        relation: Any<'py>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "clickhouse_catalog_resource_matches",
            &py_kwargs!(py; resource = resource, relation = relation, database = database)?,
        )
    }

    /// Load persisted ClickHouse deployments and publish events.
    fn load_deployment_inventory<'py>(
        slf: &Bound<'py, Self>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "load_clickhouse_deployment_inventory",
            &py_kwargs!(py; connection = slf, database = database)?,
        )
    }

    /// Render retained coverage selected by the replay window.
    fn render_replay_coverage_query<'py>(
        &self,
        py: Python<'py>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward1(py, "render_clickhouse_replay_coverage_query", request)
    }

    /// Render replay SQL from boundaries captured by this process.
    fn render_replay_from_capture<'py>(
        &self,
        py: Python<'py>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward1(py, "render_clickhouse_replay_from_capture", request)
    }

    /// Render fixed-cardinality replay against deployment-stored cutoffs.
    fn render_replay_from_deployment<'py>(
        &self,
        py: Python<'py>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward1(py, "render_clickhouse_replay_from_deployment", request)
    }

    /// Compare active and staged ClickHouse relations.
    fn compare_readiness<'py>(slf: &Bound<'py, Self>, request: Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "compare_clickhouse_readiness",
            &py_kwargs!(py; connection = slf, request = request)?,
        )
    }

    /// Render exact ClickHouse stable binding SQL.
    fn render_replace_stable_bindings<'py>(
        slf: &Bound<'py, Self>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "render_clickhouse_stable_binding_replacement",
            &py_kwargs!(py; connection = slf, request = request)?,
        )
    }

    /// Render the captured view definition so an existing view is left in place.
    fn render_restore_materialized_view<'py>(
        &self,
        definition_sql: Any<'py>,
    ) -> PyResult<Any<'py>> {
        definition_sql.call_method1(
            "replace",
            (
                "CREATE MATERIALIZED VIEW ",
                "CREATE MATERIALIZED VIEW IF NOT EXISTS ",
                1,
            ),
        )
    }

    /// Render guarded exact ClickHouse relation cleanup SQL.
    fn render_cleanup_relations<'py>(
        slf: &Bound<'py, Self>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        forward(
            py,
            "render_clickhouse_relation_cleanup",
            &py_kwargs!(py; connection = slf, request = request)?,
        )
    }

    /// Close the underlying ClickHouse connection.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let client: Any<'_> = self.raw_client(py)?;
        driver_call(py, || client.call_method0("close"))?;
        Ok(())
    }
}

/// The built-in ClickHouse adapter.
#[pyclass(module = "sqltide._native", frozen, subclass)]
pub(crate) struct ClickHouseAdapterCore;

#[pymethods]
impl ClickHouseAdapterCore {
    #[new]
    #[classmethod]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn new(
        cls: &Bound<'_, PyType>,
        _args: &Bound<'_, PyTuple>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        reject_abstract(cls)?;
        Ok(Self)
    }

    /// Return the registered ClickHouse adapter identity.
    #[getter]
    fn identity<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        model(
            py,
            "AdapterIdentity",
            &py_kwargs!(py; name = constant(py, "CLICKHOUSE_ADAPTER_NAME")?)?,
        )
    }

    /// Return the mandatory Polyglot dialect name.
    #[getter]
    fn sql_analysis_dialect<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        constant(py, "CLICKHOUSE_SQL_ANALYSIS_DIALECT")
    }

    /// Return ClickHouse's default database name.
    #[getter]
    fn default_database<'py>(&self, py: Python<'py>) -> PyResult<Any<'py>> {
        constant(py, "CLICKHOUSE_DEFAULT_DATABASE")
    }

    /// Return no schema because ClickHouse uses database-qualified relations.
    #[getter]
    fn default_schema(&self) -> Option<String> {
        None
    }

    /// Open a ClickHouse connection for the resolved configuration.
    fn connect<'py>(slf: &Bound<'py, Self>, config: Any<'py>) -> PyResult<Any<'py>> {
        let py: Python<'py> = slf.py();
        crate::clickhouse_adapter::install_unreachable_host_log_filter(py)?;
        let raw_client: Any<'py> =
            driver_call(py, || slf.call_method1("_open_raw_client", (config,)))?;
        attr(py, CONNECTION_CLASS, "ClickHouseConnection")?.call1((raw_client,))
    }

    /// Validate the exercised ClickHouse connection fields without opening a connection.
    #[pyo3(signature = (*, values, database))]
    fn build_connection_config<'py>(
        &self,
        py: Python<'py>,
        values: Any<'py>,
        database: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "build_clickhouse_connection_config",
            &py_kwargs!(py; values = values, database = database)?,
        )
    }

    /// Render one neutral resource request as ClickHouse SQL.
    #[pyo3(signature = (*, resource, database, if_not_exists = false))]
    fn render_resource<'py>(
        &self,
        py: Python<'py>,
        resource: Any<'py>,
        database: Any<'py>,
        if_not_exists: bool,
    ) -> PyResult<Any<'py>> {
        forward(
            py,
            "render_clickhouse_resource",
            &py_kwargs!(py; resource = resource, database = database, if_not_exists = if_not_exists)?,
        )
    }

    /// Map one logical source to ClickHouse resources.
    #[pyo3(signature = (*, request))]
    fn realize_source<'py>(&self, py: Python<'py>, request: Any<'py>) -> PyResult<Any<'py>> {
        forward(
            py,
            "realize_clickhouse_source",
            &py_kwargs!(py; request = request)?,
        )
    }

    /// Resolve the ClickHouse table name for one logical model.
    #[pyo3(signature = (*, logical_name))]
    fn model_relation_name(&self, py: Python<'_>, logical_name: Any<'_>) -> PyResult<String> {
        Ok(format!(
            "{}{}",
            constant(py, "CLICKHOUSE_MODEL_TABLE_NAME_PREFIX")?.str()?,
            logical_name.str()?
        ))
    }

    /// Map one semantically compiled model to ClickHouse resources.
    #[pyo3(signature = (*, request))]
    fn realize_model<'py>(&self, py: Python<'py>, request: Any<'py>) -> PyResult<Any<'py>> {
        forward(
            py,
            "realize_clickhouse_model",
            &py_kwargs!(py; request = request)?,
        )
    }

    /// Render neutral bag-comparison inputs as one ClickHouse query.
    #[pyo3(signature = (*, request))]
    fn render_set_difference_comparison<'py>(
        &self,
        py: Python<'py>,
        request: Any<'py>,
    ) -> PyResult<Any<'py>> {
        forward1(
            py,
            "render_clickhouse_set_difference_comparison",
            request.getattr("targets")?,
        )
    }

    /// Open a session-less driver client; session ids only cause SESSION_IS_LOCKED here.
    fn _open_raw_client<'py>(&self, py: Python<'py>, config: Any<'py>) -> PyResult<Any<'py>> {
        let kwargs: Kwargs<'py> = py_kwargs!(py;
            host = config.getattr("host")?,
            port = config.getattr("port")?,
            username = config.getattr("username")?,
            password = config.getattr("password")?,
        )?;
        let database: Any<'py> = config.getattr("database")?;
        if !database.is_none() {
            kwargs.set_item("database", database)?;
        }
        kwargs.set_item("autogenerate_session_id", false)?;
        kwargs.set_item(
            "settings",
            attr(py, "builtins", "dict")?.call1((config.getattr("settings")?,))?,
        )?;
        attr(py, "clickhouse_connect", "get_client")?.call((), Some(&kwargs))
    }
}

/// Index adapters by registered name, rejecting duplicate registrations.
#[pyfunction]
fn build_adapter_registry<'py>(
    py: Python<'py>,
    adapters: Any<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let registry: Bound<'py, PyDict> = PyDict::new(py);
    for adapter in adapters.try_iter()? {
        let adapter: Any<'py> = adapter?;
        let name: Any<'py> = adapter.getattr("identity")?.getattr("name")?;
        if let Some(registered) = registry.get_item(&name)? {
            return Err(adapter_error(
                py,
                "DuplicateAdapterError",
                format!(
                    "Duplicate adapter name '{}' registered by {} and {}",
                    name.str()?,
                    registered.get_type().name()?,
                    adapter.get_type().name()?
                ),
            ));
        }
        registry.set_item(name, adapter)?;
    }
    Ok(registry)
}

/// Return every adapter implementation shipped with SQLTide.
#[pyfunction]
fn builtin_adapters(py: Python<'_>) -> PyResult<Bound<'_, PyTuple>> {
    let adapter: Any<'_> = attr(
        py,
        "sqltide.adapters.clickhouse.classes.clickhouse_adapter",
        "ClickHouseAdapter",
    )?
    .call0()?;
    PyTuple::new(py, [adapter])
}

/// Return the registered adapter for a configured adapter name.
#[pyfunction]
fn resolve_adapter<'py>(py: Python<'py>, adapter_name: Any<'py>) -> PyResult<Any<'py>> {
    let registry: Bound<'py, PyDict> = attr(py, NATIVE, "build_adapter_registry")?
        .call1((attr(py, NATIVE, "builtin_adapters")?.call0()?,))?
        .cast_into()?;
    if let Some(adapter) = registry.get_item(&adapter_name)? {
        return Ok(adapter);
    }
    let names: Bound<'py, PyList> = attr(py, "builtins", "sorted")?
        .call1((&registry,))?
        .cast_into()?;
    let supported: Any<'py> = ", ".into_pyobject(py)?.call_method1("join", (names,))?;
    Err(adapter_error(
        py,
        "UnknownAdapterError",
        format!(
            "Unsupported adapter '{}'. Supported adapters: {}.",
            adapter_name.str()?,
            supported.str()?
        ),
    ))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ClickHouseConnectionCore>()?;
    module.add_class::<ClickHouseAdapterCore>()?;
    module.add_function(wrap_pyfunction!(build_adapter_registry, module)?)?;
    module.add_function(wrap_pyfunction!(builtin_adapters, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_adapter, module)?)
}
