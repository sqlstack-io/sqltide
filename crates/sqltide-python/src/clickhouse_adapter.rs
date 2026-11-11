//! The ClickHouse adapter's own decisions: neutral classification of driver errors, credential
//! placeholder resolution, how logical sources and models realize as ClickHouse resources and
//! validation of the authored connection fields.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple};
use sqltide_clickhouse::realization;

use crate::clickhouse_catalog::{build, object};
use crate::model_sql::chained;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const SECRET_PLACEHOLDER_PREFIX: &str = "__sqltide_secret__";
const SECRET_PLACEHOLDER_SUFFIX: &str = "__";
const TIMEOUT_ERROR_MARKERS: [&str; 2] = ["timeout", "timed out"];
/// How many causes or contexts of a driver error are searched for a connection failure.
const MAX_CAUSE_DEPTH: usize = 16;
/// The line clickhouse_connect logs at WARNING before raising any HTTP transport failure.
const DRIVER_HTTP_LOGGER: &str = "clickhouse_connect.driver.httpclient";
const DRIVER_HTTP_FAILURE_LOG: &str = "Unexpected Http Driver Exception";
const REFUSED_REASON: &str = "connection refused";
const UNRESOLVED_REASON: &str = "host not found";
const DEAD_LETTERS_TABLE: &str = "_sqltide_dead_letters";
const MATERIALIZED_VIEW_NAME_PREFIX: &str = "mv__";
const DEAD_LETTERS_VIEW_SUFFIX: &str = "__dead_letters";
const REPLAY_ORDER_BY: [&str; 2] = ["_replay_partition", "_replay_offset"];
const CONNECTION_CONFIG_KEYS: [&str; 5] = ["host", "port", "username", "password", "settings"];

type Value<'py> = Bound<'py, PyAny>;

fn exception<'py>(py: Python<'py>, class: &str, message: String) -> PyResult<Value<'py>> {
    py.import(ADAPTER_EXCEPTIONS)?
        .getattr(class)?
        .call1((message,))
}

fn configuration_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    Ok(PyErr::from_value(exception(
        py,
        "AdapterConfigurationError",
        message,
    )?))
}

/// Classify one ClickHouse driver error as a neutral warehouse failure.
#[pyfunction]
fn translate_clickhouse_driver_error<'py>(error: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
    let py: Python<'py> = error.py();
    let message: Bound<'py, PyString> = error.str()?;
    let normalized: String = message
        .call_method0("lower")?
        .cast_into::<PyString>()?
        .to_string_lossy()
        .into_owned();
    let text: String = message.to_string_lossy().into_owned();
    // A refused or unresolved connection never timed out, so it is checked before the timeout
    // markers, which the request URL in the message can contain as a setting name.
    let unreachable: Option<&str> = unreachable_host_reason(error)?;
    let class: &str = if unreachable.is_some() {
        "AdapterUnavailableError"
    } else if TIMEOUT_ERROR_MARKERS
        .iter()
        .any(|marker| normalized.contains(marker))
    {
        "AdapterTimeoutError"
    } else if text.contains("AUTHENTICATION_FAILED") || text.contains("Authentication failed") {
        "AdapterAuthenticationError"
    } else if text.contains("UNKNOWN_DATABASE") {
        "AdapterDatabaseNotFoundError"
    } else if text.contains("UNKNOWN_TABLE") {
        "AdapterRelationNotFoundError"
    } else {
        "AdapterWarehouseError"
    };
    let translated: Value<'py> = py
        .import(ADAPTER_EXCEPTIONS)?
        .getattr(class)?
        .call1((message,))?;
    if let Some(reason) = unreachable {
        // What the driver's own log line used to show, for the operator-facing message.
        translated.setattr("endpoint", connection_endpoint(&text))?;
        translated.setattr("reason", reason)?;
    }
    Ok(translated)
}

/// `refused` or `unresolved` reason when the driver error was caused by a refused connection or
/// an unresolvable host, as the HTTP client chains them under the driver's own error. Like a
/// traceback, it follows `__context__` only when `__cause__` is unset and the context is not
/// suppressed.
fn unreachable_host_reason(error: &Bound<'_, PyAny>) -> PyResult<Option<&'static str>> {
    let py: Python<'_> = error.py();
    let refused: Value<'_> = py.import("builtins")?.getattr("ConnectionRefusedError")?;
    let unresolved: Value<'_> = py.import("socket")?.getattr("gaierror")?;
    let mut current: Value<'_> = error.clone();
    for _ in 0..MAX_CAUSE_DEPTH {
        if current.is_instance(&refused)? {
            return Ok(Some(REFUSED_REASON));
        }
        if current.is_instance(&unresolved)? {
            return Ok(Some(UNRESOLVED_REASON));
        }
        let cause: Value<'_> = current.getattr("__cause__")?;
        current = if !cause.is_none() {
            cause
        } else if current.getattr("__suppress_context__")?.is_truthy()? {
            return Ok(None);
        } else {
            current.getattr("__context__")?
        };
        if current.is_none() {
            return Ok(None);
        }
    }
    Ok(None)
}

/// `host:port` of the HTTP connection pool a driver message names, as urllib3 renders it:
/// `HTTPConnectionPool(host='db.example.com', port=8123)`.
fn connection_endpoint(message: &str) -> Option<String> {
    let rest: &str = &message[message.find("host='")? + "host='".len()..];
    let (host, rest) = rest.split_once("', port=")?;
    let digits: usize = rest.find(|character: char| !character.is_ascii_digit())?;
    (!host.is_empty() && digits > 0).then(|| format!("{host}:{}", &rest[..digits]))
}

/// Logging filter on clickhouse_connect's HTTP client logger that demotes its generic
/// transport-failure warning to DEBUG while a refused connection or unresolvable host is being
/// handled, because SQLTide reports that failure itself. The record is dropped unless DEBUG is
/// enabled for the logger; every other record passes unchanged.
#[pyclass(module = "sqltide._native", frozen)]
pub(crate) struct UnreachableHostLogFilter;

#[pymethods]
impl UnreachableHostLogFilter {
    #[new]
    fn new() -> Self {
        Self
    }

    fn filter(&self, record: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py: Python<'_> = record.py();
        if !record.getattr("msg")?.eq(DRIVER_HTTP_FAILURE_LOG)? {
            return Ok(true);
        }
        let handled: Value<'_> = py.import("sys")?.call_method0("exc_info")?.get_item(1)?;
        if handled.is_none() || unreachable_host_reason(&handled)?.is_none() {
            return Ok(true);
        }
        let logging: Bound<'_, PyModule> = py.import("logging")?;
        let debug: Value<'_> = logging.getattr("DEBUG")?;
        let enabled: bool = logging
            .call_method1("getLogger", (record.getattr("name")?,))?
            .call_method1("isEnabledFor", (&debug,))?
            .is_truthy()?;
        if enabled {
            record.setattr("levelno", &debug)?;
            record.setattr("levelname", "DEBUG")?;
        }
        Ok(enabled)
    }
}

/// Add [`UnreachableHostLogFilter`] to clickhouse_connect's HTTP client logger once.
pub(crate) fn install_unreachable_host_log_filter(py: Python<'_>) -> PyResult<()> {
    let logger: Value<'_> = py
        .import("logging")?
        .call_method1("getLogger", (DRIVER_HTTP_LOGGER,))?;
    for existing in logger.getattr("filters")?.try_iter()? {
        if existing?.is_instance_of::<UnreachableHostLogFilter>() {
            return Ok(());
        }
    }
    logger.call_method1("addFilter", (UnreachableHostLogFilter,))?;
    Ok(())
}

/// The leftmost `__sqltide_secret__NAME__` placeholder as `(placeholder, NAME)`, matching
/// `re.escape(prefix) + r"([A-Za-z_][A-Za-z0-9_]*)" + re.escape(suffix)` with its greedy name.
fn secret_placeholder(text: &str) -> Option<(&str, &str)> {
    text.char_indices().find_map(|(start, _)| {
        let rest: &str = text[start..].strip_prefix(SECRET_PLACEHOLDER_PREFIX)?;
        let run: usize = rest
            .find(|character: char| !(character.is_ascii_alphanumeric() || character == '_'))
            .unwrap_or(rest.len());
        if rest.is_empty() || rest.as_bytes()[0].is_ascii_digit() {
            return None;
        }
        let name_length: usize = (1..run)
            .rev()
            .find(|&length| rest[length..run].starts_with(SECRET_PLACEHOLDER_SUFFIX))?;
        let end: usize =
            start + SECRET_PLACEHOLDER_PREFIX.len() + name_length + SECRET_PLACEHOLDER_SUFFIX.len();
        Some((&text[start..end], &rest[..name_length]))
    })
}

/// Substitute credential placeholders from the environment at execution time.
fn resolve_secrets<'py>(sql: Bound<'py, PyString>) -> PyResult<Bound<'py, PyString>> {
    let py: Python<'py> = sql.py();
    let environment: Value<'py> = py.import("os")?.getattr("environ")?;
    let mut resolved: Bound<'py, PyString> = sql;
    loop {
        let text: String = resolved.to_string_lossy().into_owned();
        let Some((placeholder, name)) = secret_placeholder(&text) else {
            return Ok(resolved);
        };
        let secret: Value<'py> = environment.call_method1("get", (name,))?;
        if secret.is_none() {
            return Err(PyErr::from_value(exception(
                py,
                "AdapterCapabilityError",
                format!("Credential environment variable '{name}' is not set"),
            )?));
        }
        resolved = resolved
            .call_method1("replace", (placeholder, secret))?
            .cast_into::<PyString>()?;
    }
}

/// Render one neutral adapter resource as executable ClickHouse DDL with credentials resolved.
#[pyfunction]
#[pyo3(signature = (*, resource, database, if_not_exists = false))]
fn render_clickhouse_resource<'py>(
    resource: &Bound<'py, PyAny>,
    database: &str,
    if_not_exists: bool,
) -> PyResult<Bound<'py, PyString>> {
    let py: Python<'py> = resource.py();
    let sql: String = crate::clickhouse::render_resource_with_placeholders(
        py,
        resource,
        database,
        if_not_exists,
    )?;
    resolve_secrets(PyString::new(py, &sql))
}

fn column<'py>(py: Python<'py>, name: &str, column_type: &str) -> PyResult<Value<'py>> {
    build(
        py,
        ADAPTER_MODELS,
        "AdapterColumn",
        &[
            ("name", object(py, name)?),
            ("type", object(py, column_type)?),
        ],
    )
}

/// Realize one logical source using ClickHouse resource conventions.
#[pyfunction]
#[pyo3(signature = (*, request))]
fn realize_clickhouse_source<'py>(request: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
    let py: Python<'py> = request.py();
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    if request.is_instance(&models.getattr("AdapterAdoptedSourceRealizationRequest")?)? {
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterSourceRealization",
            &[
                ("relation_name", request.getattr("relation_name")?),
                ("resources", object(py, PyTuple::empty(py))?),
            ],
        );
    }
    let logical_name: String = request.getattr("logical_name")?.extract()?;
    let source_kind: String = request.getattr("source_kind")?.extract()?;
    let format: String = request.getattr("format")?.extract()?;
    let consumer_group: Option<String> = request.getattr("consumer_group")?.extract()?;
    let project_name: Option<String> = request.getattr("project_name")?.extract()?;
    let target_name: Option<String> = request.getattr("target_name")?.extract()?;
    let setting_keys: Vec<String> = request
        .getattr("settings")?
        .try_iter()?
        .map(|setting| {
            let (key, _value): (String, Value<'py>) = setting?.extract()?;
            Ok(key)
        })
        .collect::<PyResult<_>>()?;
    let realized = match realization::realize_managed_source(&realization::ManagedSourceRequest {
        logical_name: &logical_name,
        source_kind: &source_kind,
        format: &format,
        consumer_group: consumer_group.as_deref(),
        project_name: project_name.as_deref(),
        target_name: target_name.as_deref(),
        setting_keys: setting_keys.iter().map(String::as_str).collect(),
    }) {
        Ok(realized) => realized,
        Err(message) => {
            let context: Value<'py> = PyValueError::new_err(message.clone())
                .value(py)
                .clone()
                .into_any();
            let error: Value<'py> = exception(py, "AdapterConfigurationError", message)?;
            chained(&error, &context, None)?;
            error.setattr("__suppress_context__", true)?;
            return Err(PyErr::from_value(error));
        }
    };
    let managed_source: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterManagedSource",
        &[
            ("source_kind", request.getattr("source_kind")?),
            ("name", object(py, &realized.kafka_table_name)?),
            (
                "columns",
                object(py, PyTuple::new(py, [column(py, "message", "String")?])?)?,
            ),
            ("broker_list", request.getattr("broker_list")?),
            ("topic", request.getattr("topic")?),
            ("consumer_group", object(py, &realized.consumer_group)?),
            ("format", request.getattr("format")?),
            ("settings", request.getattr("settings")?),
            (
                "naming_macro_fingerprint",
                request.getattr("naming_macro_fingerprint")?,
            ),
        ],
    )?;
    let landing_columns: Vec<Value<'py>> = realization::RAW_LANDING_COLUMNS
        .iter()
        .map(|(name, column_type)| column(py, name, column_type))
        .collect::<PyResult<_>>()?;
    let raw_table: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterTable",
        &[
            ("name", object(py, &realized.raw_table_name)?),
            ("columns", object(py, PyTuple::new(py, landing_columns)?)?),
            ("engine", object(py, "MergeTree()")?),
            ("order_by", object(py, PyTuple::new(py, REPLAY_ORDER_BY)?)?),
            ("ttl", request.getattr("ttl")?),
        ],
    )?;
    let landing_view: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterMaterializedView",
        &[
            ("name", object(py, &realized.materialized_view_name)?),
            (
                "source_relation_name",
                object(py, &realized.kafka_table_name)?,
            ),
            (
                "target_relation_name",
                object(py, &realized.raw_table_name)?,
            ),
            ("query", object(py, &realized.landing_query)?),
            ("database_template", object(py, &realized.landing_query)?),
        ],
    )?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterSourceRealization",
        &[
            ("relation_name", object(py, &realized.raw_table_name)?),
            (
                "resources",
                object(
                    py,
                    PyTuple::new(py, [managed_source, raw_table, landing_view])?,
                )?,
            ),
        ],
    )
}

/// Realize one logical model as its ClickHouse resources.
#[pyfunction]
#[pyo3(signature = (*, request))]
fn realize_clickhouse_model<'py>(request: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
    let py: Python<'py> = request.py();
    let models: Bound<'py, PyModule> = py.import(ADAPTER_MODELS)?;
    let target_relation_name: Value<'py> = request.getattr("target_relation_name")?;
    if request.is_instance(&models.getattr("AdapterViewRealizationRequest")?)? {
        let view: Value<'py> = build(
            py,
            ADAPTER_MODELS,
            "AdapterView",
            &[
                ("name", target_relation_name.clone()),
                ("query", request.getattr("resolved_query")?),
                (
                    "database_template",
                    request.getattr("resolved_database_template")?,
                ),
            ],
        )?;
        return build(
            py,
            ADAPTER_MODELS,
            "AdapterModelRealization",
            &[
                ("relation_name", target_relation_name),
                ("resources", object(py, PyTuple::new(py, [view])?)?),
            ],
        );
    }
    let view_name: String = format!(
        "{MATERIALIZED_VIEW_NAME_PREFIX}{}",
        request.getattr("logical_name")?.str()?
    );
    let dead_letter_query: Value<'py> = request.getattr("dead_letter_query")?;
    let dead_letter_database_template: Value<'py> =
        request.getattr("dead_letter_database_template")?;
    let dead_letter_view: Option<Value<'py>> =
        if dead_letter_query.is_none() || dead_letter_database_template.is_none() {
            None
        } else {
            Some(build(
                py,
                ADAPTER_MODELS,
                "AdapterMaterializedView",
                &[
                    (
                        "name",
                        object(py, format!("{view_name}{DEAD_LETTERS_VIEW_SUFFIX}"))?,
                    ),
                    (
                        "source_relation_name",
                        request.getattr("source_relation_name")?,
                    ),
                    ("target_relation_name", object(py, DEAD_LETTERS_TABLE)?),
                    ("query", dead_letter_query),
                    ("database_template", dead_letter_database_template),
                    ("dead_letters", object(py, true)?),
                ],
            )?)
        };
    let table: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterTable",
        &[
            ("name", target_relation_name.clone()),
            ("columns", request.getattr("columns")?),
            ("engine", request.getattr("engine")?),
            ("order_by", request.getattr("order_by")?),
            ("partition_by", request.getattr("partition_by")?),
            ("ttl", request.getattr("ttl")?),
            ("settings", request.getattr("settings")?),
        ],
    )?;
    let view: Value<'py> = build(
        py,
        ADAPTER_MODELS,
        "AdapterMaterializedView",
        &[
            ("name", object(py, view_name)?),
            (
                "source_relation_name",
                request.getattr("source_relation_name")?,
            ),
            ("target_relation_name", target_relation_name.clone()),
            ("query", request.getattr("resolved_query")?),
            (
                "database_template",
                request.getattr("resolved_database_template")?,
            ),
            ("refresh", request.getattr("refresh")?),
            ("append", request.getattr("append")?),
        ],
    )?;
    let resources: Vec<Value<'py>> = [Some(table), Some(view), dead_letter_view]
        .into_iter()
        .flatten()
        .collect();
    build(
        py,
        ADAPTER_MODELS,
        "AdapterModelRealization",
        &[
            ("relation_name", target_relation_name),
            ("resources", object(py, PyTuple::new(py, resources)?)?),
        ],
    )
}

/// `sorted(values)` with Python ordering.
fn sorted<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Bound<'py, PyList>> {
    let list: Bound<'py, PyList> = PyList::new(py, values)?;
    list.sort()?;
    Ok(list)
}

/// Validate optional ClickHouse session settings into ordered name/value pairs.
fn connection_settings<'py>(value: &Value<'py>) -> PyResult<Value<'py>> {
    let py: Python<'py> = value.py();
    if value.is_none() {
        return object(py, PyTuple::empty(py));
    }
    if !value.is_instance(&py.import("collections.abc")?.getattr("Mapping")?)? {
        return Err(configuration_error(
            py,
            "ClickHouse connection settings requires a table of setting values".to_owned(),
        )?);
    }
    let mut pairs: Vec<Value<'py>> = Vec::new();
    for item in value.call_method0("items")?.try_iter()? {
        let (name, setting): (Value<'py>, Value<'py>) = item?.extract()?;
        if !name.is_truthy()? {
            return Err(configuration_error(
                py,
                "ClickHouse connection settings requires non-empty setting names".to_owned(),
            )?);
        }
        let rendered: Value<'py> = if setting.is_instance_of::<PyBool>() {
            object(py, if setting.is_truthy()? { "1" } else { "0" })?
        } else if setting.is_instance_of::<PyString>()
            || setting.is_instance_of::<PyInt>()
            || setting.is_instance_of::<PyFloat>()
        {
            setting.str()?.into_any()
        } else {
            return Err(configuration_error(
                py,
                format!(
                    "ClickHouse connection setting {} requires a string, number, or boolean value",
                    name.str()?
                ),
            )?);
        };
        pairs.push(object(py, (name, rendered))?);
    }
    object(py, sorted(py, pairs)?.to_tuple())
}

/// Validate the exercised ClickHouse connection fields without opening a connection.
#[pyfunction]
#[pyo3(signature = (*, values, database))]
fn build_clickhouse_connection_config<'py>(
    values: &Bound<'py, PyAny>,
    database: Value<'py>,
) -> PyResult<Value<'py>> {
    let py: Python<'py> = values.py();
    let keys: Value<'py> = py.get_type::<PySet>().call1((values,))?;
    let unknown_keys: Value<'py> = keys.sub(PyFrozenSet::new(py, CONNECTION_CONFIG_KEYS)?)?;
    if unknown_keys.is_truthy()? {
        let names: Value<'py> = PyString::new(py, ", ").call_method1(
            "join",
            (py.import("builtins")?
                .call_method1("sorted", (unknown_keys,))?,),
        )?;
        return Err(configuration_error(
            py,
            format!("ClickHouse connection contains unsupported fields: {names}"),
        )?);
    }
    let host: Value<'py> = values.call_method1("get", ("host",))?;
    let port: Value<'py> = values.call_method1("get", ("port",))?;
    let username: Value<'py> = values.call_method1("get", ("username",))?;
    let password: Value<'py> = values.call_method1("get", ("password",))?;
    if !host.is_instance_of::<PyString>() || !host.is_truthy()? {
        return Err(configuration_error(
            py,
            "ClickHouse connection requires non-empty string host".to_owned(),
        )?);
    }
    if !port.is_instance_of::<PyInt>() {
        return Err(configuration_error(
            py,
            "ClickHouse connection requires integer port".to_owned(),
        )?);
    }
    if !username.is_instance_of::<PyString>() || !username.is_truthy()? {
        return Err(configuration_error(
            py,
            "ClickHouse connection requires non-empty string username".to_owned(),
        )?);
    }
    if !password.is_instance_of::<PyString>() {
        return Err(configuration_error(
            py,
            "ClickHouse connection requires string password".to_owned(),
        )?);
    }
    let settings: Value<'py> = connection_settings(&values.call_method1("get", ("settings",))?)?;
    build(
        py,
        ADAPTER_MODELS,
        "AdapterConnectionConfig",
        &[
            ("host", host),
            ("port", port),
            ("username", username),
            ("password", password),
            ("database", database),
            ("settings", settings),
        ],
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<UnreachableHostLogFilter>()?;
    module.add_function(wrap_pyfunction!(translate_clickhouse_driver_error, module)?)?;
    module.add_function(wrap_pyfunction!(render_clickhouse_resource, module)?)?;
    module.add_function(wrap_pyfunction!(realize_clickhouse_source, module)?)?;
    module.add_function(wrap_pyfunction!(realize_clickhouse_model, module)?)?;
    module.add_function(wrap_pyfunction!(
        build_clickhouse_connection_config,
        module
    )?)
}
