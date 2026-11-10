//! The `/api/definitions` payload: one held compile serialized for the UI, with broker
//! credentials and connection secrets redacted.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::support::attr;
use crate::py_kwargs;

const REDACTED_SECRET_PLACEHOLDER: &str = "<redacted>";
const BROKER_USERINFO_SEPARATOR: &str = "@";
const SENSITIVE_SOURCE_SETTING_FRAGMENTS: [&str; 6] =
    ["credential", "password", "private_key", "sasl", "secret", "token"];
const CONNECTION_SECRET_KEYS: [&str; 1] = ["password"];
const FALLBACK_RENDER_DATABASE: &str = "default";
const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_MODELS: &str = "sqltide.compiler.discovery.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const ANCHOR_REASON: &str = "sqltide.dev_server.types";

type Any<'py> = Bound<'py, PyAny>;

fn none(py: Python<'_>) -> Any<'_> {
    py.None().into_bound(py)
}

fn get<'py>(value: &Any<'py>, name: &str) -> PyResult<Any<'py>> {
    value.getattr(name)
}

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Any<'py>> {
    attr(py, module, name)
}

fn is<'py>(value: &Any<'py>, module: &str, name: &str) -> PyResult<bool> {
    value.is_instance(&class(value.py(), module, name)?)
}

fn str_of<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(value.str()?.into_any())
}

fn list<'py>(py: Python<'py>, items: Vec<Any<'py>>) -> PyResult<Any<'py>> {
    Ok(PyList::new(py, items)?.into_any())
}

fn as_list<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    Ok(PyList::new(value.py(), value.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any())
}

fn dict_of<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    class(value.py(), "builtins", "dict")?.call1((value,))
}

/// `resources_by_logical_key.get(key, ())`.
fn resources<'py>(analysis: &Any<'py>, key: &Any<'py>) -> PyResult<Vec<Any<'py>>> {
    let empty = PyTuple::empty(analysis.py());
    get(&get(analysis, "realized_project")?, "resources_by_logical_key")?
        .call_method1("get", (key, empty))?
        .try_iter()?
        .collect()
}

fn redacted_broker_list(broker_list: &str) -> String {
    if broker_list.contains(BROKER_USERINFO_SEPARATOR) {
        return REDACTED_SECRET_PLACEHOLDER.to_owned();
    }
    broker_list.to_owned()
}

fn redacted_source_settings<'py>(settings: &Any<'py>) -> PyResult<Any<'py>> {
    if settings.is_none() {
        return Ok(settings.clone());
    }
    let redacted: Bound<'py, PyDict> = PyDict::new(settings.py());
    for item in settings.call_method0("items")?.try_iter()? {
        let (key, value): (Any<'py>, Any<'py>) = item?.extract()?;
        let lowered: String = key.call_method0("lower")?.extract()?;
        if SENSITIVE_SOURCE_SETTING_FRAGMENTS.iter().any(|fragment| lowered.contains(fragment)) {
            redacted.set_item(key, REDACTED_SECRET_PLACEHOLDER)?;
        } else {
            redacted.set_item(key, value)?;
        }
    }
    Ok(redacted.into_any())
}

fn render_database(analysis: &Any<'_>) -> PyResult<String> {
    let configured: Option<String> =
        get(&get(&get(analysis, "compile_inputs")?, "effective_target")?, "default_database")?.extract()?;
    Ok(configured.filter(|name| !name.is_empty()).unwrap_or_else(|| FALLBACK_RENDER_DATABASE.to_owned()))
}

fn allowed_cross_pipeline_references<'py>(analysis: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let mut items: Vec<Any<'py>> = Vec::new();
    for reference in get(&get(analysis, "compiled_project")?, "allowed_cross_pipeline_references")?.try_iter()? {
        let reference = reference?;
        items.push(
            py_kwargs!(py;
                upstreamPipeline = get(&reference, "upstream_pipeline")?,
                downstreamPipeline = get(&reference, "downstream_pipeline")?,
            )?
            .into_any(),
        );
    }
    Ok(PyTuple::new(py, items)?.into_any())
}

fn dependencies<'py>(analysis: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    Ok(py_kwargs!(py;
        modelReferenceScope = str_of(&get(&get(analysis, "compiled_project")?, "model_reference_scope")?)?,
        allowedCrossPipelineReferences = allowed_cross_pipeline_references(analysis)?,
    )?
    .into_any())
}

/// Frozen nested connection values as plain mappings and lists.
fn serializable_connection_value<'py>(value: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    if value.is_instance(&class(py, "collections.abc", "Mapping")?)? {
        let nested: Bound<'py, PyDict> = PyDict::new(py);
        for item in value.call_method0("items")?.try_iter()? {
            let (key, item): (Any<'py>, Any<'py>) = item?.extract()?;
            nested.set_item(key.str()?, serializable_connection_value(&item)?)?;
        }
        return Ok(nested.into_any());
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        let items: Vec<Any<'py>> =
            value.try_iter()?.map(|item| serializable_connection_value(&item?)).collect::<PyResult<_>>()?;
        return list(py, items);
    }
    Ok(value.clone())
}

fn freshness_payload<'py>(policy: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = policy.py();
    if policy.is_none() {
        return Ok(none(py));
    }
    Ok(py_kwargs!(py; warnAfter = get(policy, "warn_after")?, errorAfter = get(policy, "error_after")?)?.into_any())
}

fn is_false(value: &Any<'_>) -> bool {
    value.is_instance_of::<pyo3::types::PyBool>() && !value.is_truthy().unwrap_or(true)
}

fn model_retention_payload<'py>(retention: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = retention.py();
    if retention.is_none() {
        return Ok(none(py));
    }
    if is_false(retention) {
        return Ok(py_kwargs!(py; disabled = true)?.into_any());
    }
    Ok(py_kwargs!(py;
        durationSeconds = get(retention, "duration_seconds")?,
        timestampColumn = get(retention, "timestamp_column")?,
        capAtColumn = get(retention, "cap_at_column")?,
        whenMissing = str_of(&get(retention, "when_missing")?)?,
    )?
    .into_any())
}

fn optional_str<'py>(value: Any<'py>) -> PyResult<Any<'py>> {
    if value.is_none() { Ok(value) } else { str_of(&value) }
}

fn kafka_retention_payload<'py>(retention: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = retention.py();
    if retention.is_none() {
        return Ok(none(py));
    }
    if is_false(retention) {
        return Ok(py_kwargs!(py; disabled = true)?.into_any());
    }
    Ok(py_kwargs!(py;
        durationSeconds = get(retention, "duration_seconds")?,
        timestamp = str_of(&get(retention, "timestamp")?)?,
        fallback = optional_str(get(retention, "fallback")?)?,
        capAt = optional_str(get(retention, "cap_at")?)?,
    )?
    .into_any())
}

fn project_payload<'py>(analysis: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let loaded = get(&get(analysis, "discovered_inputs")?, "loaded_project")?;
    let effective = if loaded.is_none() { loaded } else { get(&loaded, "effective_configuration")? };
    if effective.is_none() {
        return Ok(py_kwargs!(py;
            name = none(py),
            adapter = none(py),
            target = none(py),
            database = none(py),
            vars = PyDict::new(py),
            naming = none(py),
            defaults = none(py),
            connection = PyDict::new(py),
            auditScheduler = py_kwargs!(py; enabled = false)?,
            ui = py_kwargs!(py; timezone = "UTC")?,
            dependencies = dependencies(analysis)?,
        )?
        .into_any());
    }
    let naming = get(&effective, "naming")?;
    let defaults = get(&effective, "defaults")?;
    let audits = get(&defaults, "audits")?;
    let broker_list = get(&defaults, "kafka_broker_list")?;
    let broker_payload: Any<'py> = if broker_list.is_none() {
        none(py)
    } else {
        redacted_broker_list(&broker_list.extract::<String>()?).into_pyobject(py)?.into_any()
    };
    let connection: Bound<'py, PyDict> = PyDict::new(py);
    for item in get(&get(&effective, "connection")?, "values")?.try_iter()? {
        let (key, value): (Any<'py>, Any<'py>) = item?.extract()?;
        let key_text: String = key.extract()?;
        if CONNECTION_SECRET_KEYS.contains(&key_text.as_str()) {
            continue;
        }
        connection.set_item(key, serializable_connection_value(&value)?)?;
    }
    Ok(py_kwargs!(py;
        name = get(&effective, "name")?,
        adapter = get(&effective, "adapter")?,
        target = get(&effective, "target_name")?,
        database = get(&effective, "database")?,
        vars = dict_of(&get(&effective, "variables")?)?,
        naming = py_kwargs!(py;
            tablePrefix = get(&naming, "table_prefix")?,
            viewPrefix = get(&naming, "view_prefix")?,
        )?,
        defaults = py_kwargs!(py;
            managedSourceTtl = get(&defaults, "managed_source_ttl")?,
            modelTtl = get(&defaults, "model_ttl")?,
            sources = py_kwargs!(py;
                kafka = py_kwargs!(py;
                    retention = kafka_retention_payload(&get(&get(&get(&defaults, "sources")?, "kafka")?, "retention")?)?,
                )?,
            )?,
            models = py_kwargs!(py;
                retention = model_retention_payload(&get(&get(&defaults, "models")?, "retention")?)?,
            )?,
            kafkaBrokerList = broker_payload,
            freshness = freshness_payload(&get(&defaults, "freshness")?)?,
            audits = py_kwargs!(py;
                severity = get(&audits, "severity")?,
                cadenceSeconds = get(&audits, "cadence_seconds")?,
                warmupSeconds = get(&audits, "warmup_seconds")?,
            )?,
        )?,
        connection = connection,
        auditScheduler = py_kwargs!(py; enabled = get(&get(&effective, "audit_scheduler")?, "enabled")?)?,
        ui = py_kwargs!(py; timezone = get(&get(&effective, "ui")?, "timezone")?)?,
        dependencies = dependencies(analysis)?,
    )?
    .into_any())
}

fn render_resource<'py>(analysis: &Any<'py>, resource: &Any<'py>, database: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    get(analysis, "adapter_profile")?
        .call_method("render_resource", (), Some(&py_kwargs!(py; resource = resource, database = database)?))
}

fn managed_relation_payload<'py>(analysis: &Any<'py>, resource: &Any<'py>, database: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let exact = resource.get_type();
    let kind: &str = if exact.is(&class(py, ADAPTER_MODELS, "AdapterManagedSource")?) {
        "kafka_engine"
    } else if exact.is(&class(py, ADAPTER_MODELS, "AdapterTable")?) {
        "landing_table"
    } else if exact.is(&class(py, ADAPTER_MODELS, "AdapterMaterializedView")?) {
        "landing_mv"
    } else {
        "unknown"
    };
    let renderable: bool = is(resource, ADAPTER_MODELS, "AdapterManagedSource")?
        || is(resource, ADAPTER_MODELS, "AdapterTable")?
        || is(resource, ADAPTER_MODELS, "AdapterMaterializedView")?;
    let ddl: Any<'py> = if renderable { render_resource(analysis, resource, database)? } else { none(py) };
    let name: Any<'py> = match resource.getattr("name") {
        Ok(name) => name,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => "".into_pyobject(py)?.into_any(),
        Err(error) => return Err(error),
    };
    Ok(py_kwargs!(py; kind = kind, name = name, ddl = ddl)?.into_any())
}

/// `getattr(value, name, None)`.
fn get_or_none<'py>(value: &Any<'py>, name: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = value.py();
    match value.getattr(name) {
        Ok(found) => Ok(found),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => Ok(none(py)),
        Err(error) => Err(error),
    }
}

fn boundary_columns_payload<'py>(boundary: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = boundary.py();
    let columns = get_or_none(boundary, "columns")?;
    if columns.is_none() {
        return Ok(none(py));
    }
    let mapping: Bound<'py, PyDict> = PyDict::new(py);
    for role in ["partition", "offset", "timestamp", "landed_at", "cursor"] {
        let value = get_or_none(&columns, role)?;
        if value.is_instance_of::<pyo3::types::PyString>() {
            mapping.set_item(role, value)?;
        }
    }
    if mapping.is_empty() { Ok(none(py)) } else { Ok(mapping.into_any()) }
}

fn source_payload<'py>(analysis: &Any<'py>, source: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let step = get(source, "source")?;
    let key = get(source, "key")?;
    let relation_name = get(&get(analysis, "realized_project")?, "relation_name_by_logical_key")?.get_item(&key)?;
    let database: String = render_database(analysis)?;
    let mut managed: Vec<Any<'py>> = Vec::new();
    let mut kafka: Any<'py> = none(py);
    let mut source_ttl: Any<'py> = none(py);
    for resource in resources(analysis, &key)? {
        managed.push(managed_relation_payload(analysis, &resource, &database)?);
        if is(&resource, ADAPTER_MODELS, "AdapterManagedSource")? {
            let settings = dict_of(&get(&resource, "settings")?)?;
            let settings = if settings.is_truthy()? { settings } else { none(py) };
            let consumer_group = class(py, "sqltide._native", "database_scoped_consumer_group")?.call(
                (),
                Some(&py_kwargs!(py; consumer_group = get(&resource, "consumer_group")?, database = database.as_str())?),
            )?;
            kafka = py_kwargs!(py;
                brokerList = redacted_broker_list(&get(&resource, "broker_list")?.extract::<String>()?),
                topic = get(&resource, "topic")?,
                consumerGroup = consumer_group,
                format = get(&resource, "format")?,
                settings = redacted_source_settings(&settings)?,
            )?
            .into_any();
        }
        if is(&resource, ADAPTER_MODELS, "AdapterTable")? {
            source_ttl = get(&resource, "ttl")?;
        }
    }
    let is_kafka: bool = is(&step, DISCOVERY_MODELS, "KafkaLandingStep")?;
    let is_postgres: bool = is(&step, DISCOVERY_MODELS, "PostgresRefreshSourceStep")?;
    let boundary: Any<'py> = if is_postgres { none(py) } else { get(&step, "replay_boundary")? };
    let source_kind = class(py, DISCOVERY_TYPES, "SourceKind")?;
    let kind = get(
        &get(&source_kind, if is_kafka { "KAFKA" } else if is_postgres { "POSTGRES" } else { "STREAM_TABLE" })?,
        "value",
    )?;
    Ok(py_kwargs!(py;
        name = get(&key, "name")?,
        nameOrigin = py_kwargs!(py;
            kind = get(&step, "name_origin")?,
            macro = get_or_none(&step, "naming_macro")?,
            macroFingerprint = get_or_none(&step, "naming_macro_fingerprint")?,
        )?,
        kind = kind,
        boundaryMode = str_of(&get(source, "effective_replay_lineage_mode")?)?,
        relationName = relation_name,
        managedRelations = list(py, managed)?,
        ttl = source_ttl,
        retention = if is_kafka { kafka_retention_payload(&get(&get(&step, "kafka")?, "retention")?)? } else { none(py) },
        kafka = kafka,
        columnMapping = boundary_columns_payload(&boundary)?,
        freshness = freshness_payload(&get(&step, "freshness")?)?,
        refresh = if is_postgres { get(&step, "refresh")? } else { none(py) },
    )?
    .into_any())
}

fn pipeline_payload<'py>(pipeline: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = pipeline.py();
    let declared = get(pipeline, "pipeline")?;
    let protection = get(&declared, "protection")?;
    let source = get(pipeline, "source")?;
    let naming = get(&declared, "naming")?;
    let audit_defaults = get(&declared, "audit_defaults")?;
    let models: Vec<Any<'py>> = get(pipeline, "models")?
        .try_iter()?
        .map(|model| get(&get(&model?, "key")?, "name"))
        .collect::<PyResult<_>>()?;
    let name: String = get(&declared, "name")?.extract()?;
    Ok(py_kwargs!(py;
        name = name.as_str(),
        mode = str_of(&get(&declared, "mode")?)?,
        sourceName = if source.is_none() { none(py) } else { get(&get(&source, "key")?, "name")? },
        boundaryMode = str_of(&get(pipeline, "effective_replay_lineage_mode")?)?,
        models = list(py, models)?,
        directory = format!("pipelines/{name}"),
        naming = py_kwargs!(py;
            tablePrefix = get(&naming, "table_prefix")?,
            viewPrefix = get(&naming, "view_prefix")?,
        )?,
        protection = if protection.is_none() {
            none(py)
        } else {
            py_kwargs!(py;
                warning = get(&protection, "warning")?,
                confirmation = get(&protection, "confirmation")?,
            )?
            .into_any()
        },
        auditDefaults = py_kwargs!(py;
            severity = get(&audit_defaults, "severity")?,
            cadenceSeconds = get(&audit_defaults, "cadence_seconds")?,
            warmupSeconds = get(&audit_defaults, "warmup_seconds")?,
        )?,
    )?
    .into_any())
}

fn table_or_view<'py>(model: &Any<'py>) -> PyResult<Option<Any<'py>>> {
    if is(model, COMPILE_MODELS, "CompiledTableModel")? {
        return Ok(Some(get(model, "transform")?));
    }
    if is(model, COMPILE_MODELS, "CompiledViewModel")? {
        return Ok(Some(get(model, "view")?));
    }
    Ok(None)
}

fn effective_model_retention_payload<'py>(model: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = model.py();
    if !is(model, COMPILE_MODELS, "CompiledTableModel")? {
        return Ok(none(py));
    }
    let retention = get(model, "retention")?;
    let origin = get(&retention, "origin")?;
    if origin.is_none() {
        return Ok(none(py));
    }
    let value = get(&retention, "value")?;
    let payload = model_retention_payload(&value)?;
    if payload.is_none() {
        return Ok(payload);
    }
    let status: &str = if is_false(&value) {
        "disabled"
    } else if get(model, "retention_applied")?.is_truthy()? {
        "applied"
    } else {
        "skipped"
    };
    let merged = payload.cast_into::<PyDict>()?;
    merged.set_item("origin", origin.str()?)?;
    merged.set_item("status", status)?;
    Ok(merged.into_any())
}

fn column_payloads<'py>(model: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = model.py();
    let described: Bound<'py, PyDict> = PyDict::new(py);
    if let Some(authored) = table_or_view(model)? {
        for spec in get(&authored, "columns")?.try_iter()? {
            let spec = spec?;
            described.set_item(get(&spec, "name")?, get(&spec, "description")?)?;
        }
    }
    let mut columns: Vec<Any<'py>> = Vec::new();
    for column in get(model, "output_columns")?.try_iter()? {
        let column = column?;
        let name = get(&column, "name")?;
        let description = described.get_item(&name)?.unwrap_or_else(|| none(py));
        columns.push(py_kwargs!(py; name = name, type = get(&column, "type")?, description = description)?.into_any());
    }
    list(py, columns)
}

fn storage_payload<'py>(model: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = model.py();
    if !is(model, COMPILE_MODELS, "CompiledTableModel")? {
        return Ok(none(py));
    }
    let transform = get(model, "transform")?;
    let settings = get(&transform, "settings")?;
    Ok(py_kwargs!(py;
        engine = get(&transform, "engine")?,
        orderBy = as_list(&get(&transform, "order_by")?)?,
        partitionBy = get(&transform, "partition_by")?,
        ttl = get(&transform, "ttl")?,
        settings = if settings.is_none() { settings } else { dict_of(&settings)? },
    )?
    .into_any())
}

fn anchor_reason<'py>(model: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = model.py();
    let reasons = class(py, ANCHOR_REASON, "ReplayAnchorReason")?;
    let reason: &str = if !is(model, COMPILE_MODELS, "CompiledTableModel")? {
        "VIEW"
    } else if get(&get(model, "transform")?, "replay_anchor")?
        .eq(get(&class(py, DISCOVERY_TYPES, "ReplayAnchorMode")?, "NEVER")?)?
    {
        "NEVER"
    } else if get(model, "has_mutable_refs")?.is_truthy()? {
        "MUTABLE_REF"
    } else if get(model, "has_aggregate_semantics")?.is_truthy()? {
        "AGGREGATE"
    } else if !get(model, "preserves_required_lineage")?.is_truthy()? {
        "LINEAGE_LOSS"
    } else {
        "ELIGIBLE"
    };
    get(&reasons, reason)
}

fn sql_payload<'py>(analysis: &Any<'py>, model: &Any<'py>, database: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let mut authored: Any<'py> = none(py);
    if let Some(declared) = table_or_view(model)? {
        let source_path = get(&declared, "source_file_path")?;
        if !source_path.is_none() {
            for discovered in get(&get(analysis, "discovered_inputs")?, "model_files")?.try_iter()? {
                let discovered = discovered?;
                if get(&discovered, "file_path")?.eq(&source_path)? {
                    authored = get(&discovered, "contents")?;
                    break;
                }
            }
        }
    }
    let ddl: Bound<'py, PyDict> = py_kwargs!(py; table = none(py), materializedView = none(py), view = none(py))?;
    let fields: [(&str, &str); 3] =
        [("AdapterTable", "table"), ("AdapterMaterializedView", "materializedView"), ("AdapterView", "view")];
    let key = get(model, "key")?;
    for resource in resources(analysis, &key)? {
        let exact = resource.get_type();
        for (type_name, field) in fields {
            if exact.is(&class(py, ADAPTER_MODELS, type_name)?) {
                ddl.set_item(field, render_resource(analysis, &resource, database)?)?;
            }
        }
    }
    let compiled = get(&get(analysis, "realized_project")?, "resolved_query_by_model_key")?.call_method1("get", (&key,))?;
    Ok(py_kwargs!(py; authored = authored, compiled = compiled, ddl = ddl)?.into_any())
}

fn model_payload<'py>(analysis: &Any<'py>, model: &Any<'py>, database: &str) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let key = get(model, "key")?;
    let edges: Vec<Any<'py>> = get(&get(analysis, "graph")?, "upstream_edges_by_key")?
        .call_method1("get", (&key, PyTuple::empty(py)))?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let source_type = get(&class(py, "sqltide.compiler.compile.types", "LogicalResourceType")?, "SOURCE")?;
    let driving_type = get(&class(py, "sqltide.compiler.graph.types", "DependencyEdgeType")?, "DRIVING_INPUT")?;
    let mut refs: Vec<Any<'py>> = Vec::new();
    let mut driving: Any<'py> = none(py);
    for edge in &edges {
        let upstream = get(edge, "upstream_key")?;
        let edge_type = get(edge, "edge_type")?;
        refs.push(
            py_kwargs!(py;
                name = get(&upstream, "name")?,
                type = edge_type.str()?,
                isSource = get(&upstream, "resource_type")?.eq(&source_type)?,
            )?
            .into_any(),
        );
    }
    for edge in &edges {
        if get(edge, "edge_type")?.eq(&driving_type)? {
            driving = get(&get(edge, "upstream_key")?, "name")?;
        }
    }
    let description: Any<'py> = match table_or_view(model)? {
        Some(declared) => get(&declared, "description")?,
        None => none(py),
    };
    let mut mv_relation: Any<'py> = none(py);
    for resource in resources(analysis, &key)? {
        if is(&resource, ADAPTER_MODELS, "AdapterMaterializedView")? {
            mv_relation = get(&resource, "name")?;
            break;
        }
    }
    Ok(py_kwargs!(py;
        name = get(&key, "name")?,
        pipeline = get(model, "pipeline_name")?,
        kind = str_of(&get(model, "kind")?)?,
        description = description,
        relationName = get(&get(analysis, "realized_project")?, "relation_name_by_logical_key")?.get_item(&key)?,
        mvRelationName = mv_relation,
        drivingInput = driving,
        refs = list(py, refs)?,
        columns = column_payloads(model)?,
        storage = storage_payload(model)?,
        retention = effective_model_retention_payload(model)?,
        anchor = str_of(&anchor_reason(model)?)?,
        isAggregate = get(model, "has_aggregate_semantics")?,
        sql = sql_payload(analysis, model, database)?,
    )?
    .into_any())
}

fn identity_payload<'py>(identity: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = identity.py();
    if identity.is_none() {
        return Ok(none(py));
    }
    Ok(py_kwargs!(py;
        bindingKey = get(identity, "binding_key")?,
        definitionFingerprint = get(identity, "definition_fingerprint")?,
        executionFingerprint = get(identity, "execution_fingerprint")?,
    )?
    .into_any())
}

/// `value or fallback`.
fn or<'py>(value: Any<'py>, fallback: impl FnOnce() -> PyResult<Any<'py>>) -> PyResult<Any<'py>> {
    if value.is_truthy()? { Ok(value) } else { fallback() }
}

fn audit_payload<'py>(audit: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = audit.py();
    let file_path = get(audit, "file_path")?;
    Ok(py_kwargs!(py;
        name = or(get(audit, "name")?, || get(&file_path, "stem"))?,
        file = file_path.str()?,
        severity = get(audit, "severity")?,
        description = get(audit, "description")?,
        genericName = get(audit, "generic_definition_name")?,
        referencedModels = as_list(&get(audit, "referenced_model_names")?)?,
        sql = get(audit, "query")?,
        policy = py_kwargs!(py;
            cadenceSeconds = get(audit, "cadence_seconds")?,
            warmupSeconds = get(audit, "warmup_seconds")?,
            scheduled = get(audit, "scheduled")?,
        )?,
        identity = identity_payload(&get(audit, "quality_identity")?)?,
    )?
    .into_any())
}

fn test_payload<'py>(test_case: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = test_case.py();
    let file_path = get(test_case, "file_path")?;
    let targets: Vec<Any<'py>> = get(test_case, "target_cases")?
        .try_iter()?
        .map(|target| get(&target?, "target_model_name"))
        .collect::<PyResult<_>>()?;
    Ok(py_kwargs!(py;
        name = or(get(test_case, "name")?, || get(&file_path, "stem"))?,
        file = file_path.str()?,
        targets = list(py, targets)?,
        sql = get(test_case, "query")?,
        identity = identity_payload(&get(test_case, "quality_identity")?)?,
    )?
    .into_any())
}

fn macro_payloads<'py>(analysis: &Any<'py>) -> PyResult<Any<'py>> {
    let py: Python<'py> = analysis.py();
    let macros = get(&get(&get(analysis, "compiled_project")?, "macro_registry")?, "macros")?;
    let ordered = class(py, "builtins", "sorted")?.call1((macros.call_method0("items")?,))?;
    let mut payloads: Vec<Any<'py>> = Vec::new();
    for item in ordered.try_iter()? {
        let (name, declared): (Any<'py>, Any<'py>) = item?.extract()?;
        payloads.push(
            py_kwargs!(py;
                name = name,
                file = get(&declared, "relative_path")?.str()?,
                source = get(&declared, "source")?,
                description = get(&declared, "description")?,
            )?
            .into_any(),
        );
    }
    list(py, payloads)
}

/// Build the complete static definitions payload for one held compile.
#[pyfunction]
#[pyo3(signature = (*, analysis, version_key))]
pub(crate) fn build_definitions_payload<'py>(
    py: Python<'py>,
    analysis: Any<'py>,
    version_key: Any<'py>,
) -> PyResult<Any<'py>> {
    let database: String = render_database(&analysis)?;
    let compiled = get(&analysis, "compiled_project")?;
    let sources: Vec<Any<'py>> = get(&compiled, "sources")?
        .try_iter()?
        .map(|source| source_payload(&analysis, &source?))
        .collect::<PyResult<_>>()?;
    let pipelines: Vec<Any<'py>> =
        get(&compiled, "pipelines")?.try_iter()?.map(|pipeline| pipeline_payload(&pipeline?)).collect::<PyResult<_>>()?;
    let models: Vec<Any<'py>> = get(&compiled, "models")?
        .try_iter()?
        .map(|model| model_payload(&analysis, &model?, &database))
        .collect::<PyResult<_>>()?;
    let audits: Vec<Any<'py>> =
        get(&compiled, "audits")?.try_iter()?.map(|audit| audit_payload(&audit?)).collect::<PyResult<_>>()?;
    let tests: Vec<Any<'py>> =
        get(&compiled, "test_cases")?.try_iter()?.map(|test_case| test_payload(&test_case?)).collect::<PyResult<_>>()?;
    Ok(py_kwargs!(py;
        versionKey = version_key,
        project = project_payload(&analysis)?,
        sources = list(py, sources)?,
        pipelines = list(py, pipelines)?,
        models = list(py, models)?,
        audits = list(py, audits)?,
        tests = list(py, tests)?,
        macros = macro_payloads(&analysis)?,
    )?
    .into_any())
}
