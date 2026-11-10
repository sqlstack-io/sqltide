//! Committed-offset resets for managed sources gaining a fresh landing table: which sources
//! qualify, the admin client configuration, the scoped consumer group and the operator notice.
//! The kafka-python client itself is driven by its Python glue module.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use sqltide_clickhouse::resources::database_scoped_consumer_group;
use sqltide_compile::relations::{KAFKA_TABLE_NAME_PREFIX, RAW_TABLE_NAME_PREFIX};

use crate::dev_server::support::attr;
use crate::py_kwargs;

const GLUE: &str = "sqltide.executor.kafka_admin._helpers.offset_reset";
const MODELS: &str = "sqltide.executor.kafka_admin.models";
const API_VERSION_AUTO_TIMEOUT_MS: i64 = 1_000;
const REQUEST_TIMEOUT_MS: i64 = 5_000;
const SECURITY_PROTOCOL: &str = "security_protocol";
/// Source settings and the kafka-python admin options they configure.
const TRANSLATED_SETTINGS: [(&str, &str); 4] = [
    ("kafka_security_protocol", SECURITY_PROTOCOL),
    ("kafka_sasl_mechanism", "sasl_mechanism"),
    ("kafka_sasl_username", "sasl_plain_username"),
    ("kafka_sasl_password", "sasl_plain_password"),
];

type Any<'py> = Bound<'py, PyAny>;

fn client_config<'py>(broker_list: &Any<'py>, settings: &Any<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py: Python<'py> = broker_list.py();
    let servers: Bound<'py, PyList> = PyList::empty(py);
    for item in broker_list.call_method1("split", (",",))?.try_iter()? {
        servers.append(item?.call_method0("strip")?)?;
    }
    let config: Bound<'py, PyDict> = PyDict::new(py);
    config.set_item("bootstrap_servers", servers)?;
    config.set_item("request_timeout_ms", REQUEST_TIMEOUT_MS)?;
    config.set_item("api_version_auto_timeout_ms", API_VERSION_AUTO_TIMEOUT_MS)?;
    for (source, target) in TRANSLATED_SETTINGS {
        let value: Any<'py> = settings.call_method1("get", (source,))?;
        if !value.is_none() {
            let value: Any<'py> = if target == SECURITY_PROTOCOL {
                value.call_method0("upper")?
            } else {
                value
            };
            config.set_item(target, value)?;
        }
    }
    Ok(config)
}

fn notice(
    consumer_group: &str,
    landing: &str,
    deleted: bool,
    error: Option<&str>,
) -> Option<String> {
    if let Some(error) = error {
        return Some(format!(
            "Could not reset committed offsets for consumer group '{consumer_group}' ahead of \
             creating {landing}: {error}. If this landing table replaces a previous one, \
             ingestion may resume from stale offsets; reset the group manually."
        ));
    }
    deleted.then(|| {
        format!(
            "Reset committed offsets for consumer group '{consumer_group}' ahead of creating \
             {landing}; ingestion starts from the earliest retained messages."
        )
    })
}

/// Reset committed offsets for every managed source gaining a fresh landing table.
#[pyfunction]
#[pyo3(signature = (*, source_preparation, client_factory = None))]
fn reset_fresh_landing_offsets<'py>(
    py: Python<'py>,
    source_preparation: Any<'py>,
    client_factory: Option<Any<'py>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let created: Vec<String> = source_preparation
        .getattr("created_relation_names")?
        .try_iter()?
        .map(|name| name?.extract())
        .collect::<PyResult<_>>()?;
    let client_factory: Any<'py> = match client_factory {
        Some(factory) => factory,
        None => attr(py, "kafka", "KafkaAdminClient")?,
    };
    let mut resets: Vec<Any<'py>> = Vec::new();
    for managed in source_preparation.getattr("managed_sources")?.try_iter()? {
        let managed: Any<'py> = managed?;
        let resource: Any<'py> = managed.getattr("resource")?;
        let name: String = resource.getattr("name")?.extract()?;
        let landing: String = format!(
            "{RAW_TABLE_NAME_PREFIX}{}",
            name.strip_prefix(KAFKA_TABLE_NAME_PREFIX).unwrap_or(&name)
        );
        if !created.contains(&landing) {
            continue;
        }
        let consumer_group: String = database_scoped_consumer_group(
            &resource.getattr("consumer_group")?.extract::<String>()?,
            &managed.getattr("database")?.extract::<String>()?,
        );
        let broker_list: Any<'py> = resource.getattr("broker_list")?;
        let settings: Any<'py> =
            attr(py, "builtins", "dict")?.call1((resource.getattr("settings")?,))?;
        // Like the admin client itself, a configuration that cannot be built is reported in the
        // notice rather than failing the build.
        let (deleted, error): (bool, Option<String>) = match client_config(&broker_list, &settings)
        {
            Ok(config) => attr(py, GLUE, "delete_consumer_group")?
                .call(
                    (),
                    Some(&py_kwargs!(py;
                        client_factory = &client_factory,
                        config = config,
                        consumer_group = &consumer_group,
                    )?),
                )?
                .extract()?,
            Err(failure) if failure.is_instance_of::<PyException>(py) => {
                (false, Some(failure.value(py).str()?.extract()?))
            }
            Err(failure) => return Err(failure),
        };
        let notice: Option<String> = notice(&consumer_group, &landing, deleted, error.as_deref());
        resets.push(attr(py, MODELS, "ConsumerGroupOffsetReset")?.call(
            (),
            Some(&py_kwargs!(py;
                consumer_group = consumer_group,
                landing_relation_name = landing,
                deleted = deleted,
                error = error,
                notice = notice,
            )?),
        )?);
    }
    PyTuple::new(py, resets)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(reset_fresh_landing_offsets, module)?)
}
