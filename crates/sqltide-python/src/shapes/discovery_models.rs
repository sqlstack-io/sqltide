//! Authored project specification shapes and the discovery results built from them.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use super::{Coerce, Given, add, add_class, coerce, factory, raised, shapes};
use crate::project_discovery::immutable_config_pairs;

const CONFIG_PAIRS: Coerce = Coerce::With(config_pairs);
const READ_ONLY_MAPPING: Coerce = Coerce::Maybe(&Coerce::ReadOnlyMapping);
const ROW_ERROR_POLICY: Coerce = Coerce::Maybe(&Coerce::Enum("RowErrorPolicy"));
const BOUNDED_REPLAY_FALLBACK: Coerce = Coerce::Maybe(&Coerce::Enum("BoundedReplayFallback"));
const RETENTION_REFERENCE: Coerce = Coerce::Enum("KafkaRetentionReference");
const MAYBE_RETENTION_REFERENCE: Coerce = Coerce::Maybe(&RETENTION_REFERENCE);
const MAYBE_RETENTION_ORIGIN: Coerce = Coerce::Maybe(&Coerce::Enum("KafkaRetentionOrigin"));
const MAYBE_PIPELINE_MODE: Coerce = Coerce::Maybe(&Coerce::Enum("PipelineMode"));
const SOURCE_KIND: Coerce = Coerce::Enum("SourceKind");
const SOURCE_NAME_ORIGIN: Coerce = Coerce::Enum("SourceNameOrigin");
const QUERY_OR_SQL_FILE: &str = "Exactly one of 'query' or 'sql_file' must be provided";

/// Configuration pairs frozen by `immutable_config_pairs`, in authored order.
fn config_pairs<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    Ok(immutable_config_pairs(value.py(), value.clone())?.into_any())
}

/// The bound value at `index`.
fn at<'a, 'py>(values: &'a [Bound<'py, PyAny>], index: usize) -> PyResult<&'a Bound<'py, PyAny>> {
    values
        .get(index)
        .ok_or_else(|| PyValueError::new_err("shape bound too few values"))
}

/// Normalizes the values at the given indexes, in the order the dataclass's `__post_init__` did.
fn coerce_in_order(values: &mut [Bound<'_, PyAny>], rules: &[(usize, Coerce)]) -> PyResult<()> {
    for (index, rule) in rules {
        let slot = values
            .get_mut(*index)
            .ok_or_else(|| PyValueError::new_err("shape bound too few values"))?;
        let normalized = coerce(slot.py(), *rule, slot)?;
        *slot = normalized;
    }
    Ok(())
}

/// Exactly one of the query and the SQL file is given.
fn query_or_sql_file(query: &Bound<'_, PyAny>, sql_file: &Bound<'_, PyAny>) -> PyResult<()> {
    if query.is_truthy()? == sql_file.is_truthy()? {
        return Err(raised(
            query.py(),
            "ProjectSpecError",
            QUERY_OR_SQL_FILE.to_owned(),
        )?);
    }
    Ok(())
}

/// `TransformStep`: normalized policies and settings, one query source and an ORDER BY.
fn transform_step(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    coerce_in_order(
        values,
        &[
            (21, ROW_ERROR_POLICY),
            (3, Coerce::Tuple),
            (12, READ_ONLY_MAPPING),
            (14, Coerce::Enum("ReplayAnchorMode")),
            (16, BOUNDED_REPLAY_FALLBACK),
        ],
    )?;
    query_or_sql_file(at(values, 8)?, at(values, 9)?)?;
    let order_by: &Bound<'_, PyAny> = at(values, 3)?;
    if !order_by.is_truthy()? {
        return Err(raised(
            order_by.py(),
            "ProjectSpecError",
            "TransformStep must declare at least one ORDER BY expression".to_owned(),
        )?);
    }
    Ok(())
}

/// `ViewStep`: one query source.
fn view_step(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    query_or_sql_file(at(values, 1)?, at(values, 2)?)
}

/// `Pipeline`: the row error policy, transforms, mode and replay fallback normalized in order.
fn pipeline(values: &mut [Bound<'_, PyAny>]) -> PyResult<()> {
    coerce_in_order(
        values,
        &[
            (11, ROW_ERROR_POLICY),
            (2, Coerce::Tuple),
            (3, Coerce::Enum("PipelineMode")),
            (5, BOUNDED_REPLAY_FALLBACK),
        ],
    )
}

shapes! {
    module = "sqltide.compiler.discovery.models";

    /// One project source file loaded exactly once during discovery.
    DiscoveredProjectFile @no_repr { file_path, relative_path, contents }

    /// Unexpanded adapter-owned connection values retained without secret repr output.
    RawConnectionConfig @no_repr { values(default = Given::EmptyTuple, coerce = CONFIG_PAIRS) }

    /// Effective target-resolved background audit scheduler configuration.
    AuditSchedulerConfig { enabled(default = Given::Bool(false)) }

    /// Optional authored scheduler override at target scope.
    AuditSchedulerOverride { enabled(default = Given::None) }

    /// Effective target-resolved sensor dispatcher configuration.
    SensorAutomationConfig {
        enabled(default = Given::Bool(false)),
        tick_retention_days(default = Given::Int(0)),
        maximum_event_age_seconds(default = Given::None),
    }

    /// Optional authored sensor automation override at target scope.
    SensorAutomationOverride {
        enabled(default = Given::None),
        tick_retention_days(default = Given::None),
        maximum_event_age_seconds(default = Given::None),
    }

    /// Committed absolute limits applied before a build mutates its target.
    BuildConfig { max_pipelines(default = Given::None) }

    /// Committed finite ClickHouse table DROP ceiling for one target.
    DestructionConfig { max_table_size_to_drop_bytes(default = Given::None) }

    /// Committed browser presentation settings.
    UiConfig { timezone(default = Given::Str("UTC")) }

    /// One directional exception to pipeline-local model ownership.
    AllowedCrossPipelineReference { upstream_pipeline, downstream_pipeline }

    /// Committed project dependency-boundary policy.
    ProjectDependencies {
        model_reference_scope(default = Given::Member("ModelReferenceScope", "PROJECT"), coerce = Coerce::Enum("ModelReferenceScope")),
        allowed_cross_pipeline_references(default = Given::EmptyTuple, coerce = Coerce::Tuple),
    }

    /// One committed named target before local resolution.
    ProjectTarget @no_repr {
        database(default = Given::None),
        connection(default = Given::Factory(factory::<RawConnectionConfig>)),
        variables(default = Given::EmptyTuple, coerce = CONFIG_PAIRS),
        audit_scheduler(default = Given::Factory(factory::<AuditSchedulerOverride>)),
        sensors(default = Given::Factory(factory::<SensorAutomationOverride>)),
        build(default = Given::Factory(factory::<BuildConfig>)),
        destruction(default = Given::Factory(factory::<DestructionConfig>)),
        production(default = Given::Bool(false)),
    }

    /// One local named target definition or override.
    LocalProjectTarget @no_repr {
        database(default = Given::None),
        connection(default = Given::Factory(factory::<RawConnectionConfig>)),
        variables(default = Given::EmptyTuple, coerce = CONFIG_PAIRS),
        audit_scheduler(default = Given::Factory(factory::<AuditSchedulerOverride>)),
        sensors(default = Given::Factory(factory::<SensorAutomationOverride>)),
    }

    /// One breaking or non-breaking replay policy choice.
    ReplayOnChangeRule {
        mode(coerce = Coerce::Enum("ReplayOnChangeMode")),
        lookback_seconds(default = Given::None),
    }

    /// Replay scope selected independently for breaking and non-breaking changes.
    ReplayOnChangePolicy { breaking(default = Given::None), non_breaking(default = Given::None) }

    /// Authored freshness thresholds as `<int><s|m|h|d>` durations.
    SourceFreshnessPolicy { warn_after(default = Given::None), error_after(default = Given::None) }

    /// Optional project- or pipeline-level audit policy defaults.
    AuditDefaults {
        severity(default = Given::None),
        cadence_seconds(default = Given::None),
        warmup_seconds(default = Given::None),
    }

    /// Project-wide virtual deployment comparison thresholds.
    DeploymentReadinessDefaults {
        maximum_lag_seconds(default = Given::Float(30.0)),
        minimum_staged_row_ratio(default = Given::Float(0.5)),
    }

    /// Project-wide defaults for managed Kafka sources.
    KafkaSourceDefaults {
        naming_macro(default = Given::None),
        retention(default = Given::None),
        retention_template(default = Given::None, compare = false, repr = false),
    }

    /// Typed retention applied after a table model's output schema is known.
    ModelRetentionPolicy {
        duration_seconds,
        timestamp_column,
        cap_at_column(default = Given::None),
        when_missing(default = Given::Member("RetentionMissingBehavior", "ERROR"), coerce = Coerce::Enum("RetentionMissingBehavior")),
    }

    /// Typed retention over SQLTide's fixed managed Kafka replay schema.
    KafkaRetentionPolicy {
        duration_seconds,
        timestamp(default = Given::Member("KafkaRetentionReference", "BROKER"), coerce = RETENTION_REFERENCE),
        fallback(default = Given::Member("KafkaRetentionReference", "LANDED"), coerce = RETENTION_REFERENCE),
        cap_at(default = Given::None, coerce = MAYBE_RETENTION_REFERENCE),
    }

    /// Project- or pipeline-wide table-model defaults.
    ModelDefaults {
        retention(default = Given::None),
        retention_template(default = Given::None, compare = false, repr = false),
        on_row_error(default = Given::None, coerce = ROW_ERROR_POLICY),
    }

    /// Project-wide defaults grouped by source kind.
    SourceDefaults { kafka(default = Given::Factory(factory::<KafkaSourceDefaults>)) }

    /// Committed project-wide authored defaults.
    ProjectDefaults {
        managed_source_ttl(default = Given::None),
        model_ttl(default = Given::None),
        kafka_broker_list(default = Given::None),
        pipeline_mode(default = Given::Member("PipelineMode", "DIRECT"), coerce = Coerce::Enum("PipelineMode")),
        replay_on_change(default = Given::None),
        bounded_replay_fallback(default = Given::None),
        run_presumed_failed_after_seconds(default = Given::Int(600)),
        freshness(default = Given::None),
        audits(default = Given::Factory(factory::<AuditDefaults>)),
        deployment_readiness(default = Given::Factory(factory::<DeploymentReadinessDefaults>)),
        sources(default = Given::Factory(factory::<SourceDefaults>)),
        models(default = Given::Factory(factory::<ModelDefaults>)),
    }

    /// Local overrides for project-wide pipeline defaults.
    LocalProjectDefaults { pipeline_mode(default = Given::None, coerce = MAYBE_PIPELINE_MODE) }

    /// Project-wide pipeline validation and model relation naming.
    ProjectNaming {
        pipeline_prefix(default = Given::Str("pl__")),
        pipeline_naming_macro(default = Given::None),
        table_prefix(default = Given::Str("tbl__")),
        view_prefix(default = Given::Str("view__")),
    }

    /// Optional pipeline overrides for project model relation prefixes.
    PipelineNaming { table_prefix(default = Given::None), view_prefix(default = Given::None) }

    /// Operator warning and exact confirmation required before a protected build.
    PipelineProtection { warning, confirmation }

    /// Committed TOML project configuration before invocation resolution.
    AuthoredProjectConfig @no_repr {
        name,
        adapter,
        default_target,
        connection,
        variables(coerce = CONFIG_PAIRS),
        targets,
        defaults(default = Given::Factory(factory::<ProjectDefaults>)),
        naming(default = Given::Factory(factory::<ProjectNaming>)),
        audit_scheduler(default = Given::Factory(factory::<AuditSchedulerConfig>)),
        sensors(default = Given::Factory(factory::<SensorAutomationConfig>)),
        build(default = Given::Factory(factory::<BuildConfig>)),
        ui(default = Given::Factory(factory::<UiConfig>)),
        dependencies(default = Given::Factory(factory::<ProjectDependencies>)),
    }

    /// Optional local TOML overrides before invocation resolution.
    LocalProjectConfig @no_repr {
        target(default = Given::None),
        adapter(default = Given::None),
        defaults(default = Given::Factory(factory::<LocalProjectDefaults>)),
        connection(default = Given::Factory(factory::<RawConnectionConfig>)),
        variables(default = Given::EmptyTuple, coerce = CONFIG_PAIRS),
        targets(default = Given::EmptyTuple),
    }

    /// Retained committed and optional local configuration inputs.
    LoadedProjectConfiguration @no_repr { project, local, project_source, local_source }

    /// One immutable connection-lazy project configuration for an invocation.
    EffectiveProjectConfiguration @no_repr {
        name,
        adapter,
        target_name,
        database,
        connection,
        variables(coerce = CONFIG_PAIRS),
        defaults,
        naming(default = Given::Factory(factory::<ProjectNaming>)),
        audit_scheduler(default = Given::Factory(factory::<AuditSchedulerConfig>)),
        sensors(default = Given::Factory(factory::<SensorAutomationConfig>)),
        build(default = Given::Factory(factory::<BuildConfig>)),
        destruction(default = Given::Factory(factory::<DestructionConfig>)),
        ui(default = Given::Factory(factory::<UiConfig>)),
        production_target(default = Given::Bool(false)),
        dependencies(default = Given::Factory(factory::<ProjectDependencies>)),
    }

    /// Kafka engine settings for a landing step.
    KafkaSettings @no_repr {
        broker_list,
        topic,
        consumer_group(default = Given::None),
        format(default = Given::Str("JSONAsString")),
        ttl(default = Given::None),
        settings(default = Given::None, coerce = READ_ONLY_MAPPING),
        retention(default = Given::None),
        retention_origin(default = Given::None, coerce = MAYBE_RETENTION_ORIGIN),
    }

    /// A standardized Kafka landing step.
    KafkaLandingStep @no_repr {
        name,
        kafka,
        replay_boundary(default = Given::None),
        freshness(default = Given::None),
        name_origin(default = Given::Member("SourceNameOrigin", "EXPLICIT"), coerce = SOURCE_NAME_ORIGIN),
        naming_macro(default = Given::None),
        naming_macro_fingerprint(default = Given::None),
    }

    /// User-declared source boundary column mapping.
    ReplayBoundaryColumns {
        partition(default = Given::None),
        offset(default = Given::None),
        timestamp(default = Given::None),
        landed_at(default = Given::None),
        cursor(default = Given::None),
    }

    /// User-declared replay boundary contract for an adopted source.
    ReplayBoundary { mode(coerce = Coerce::Enum("ReplayBoundaryMode")), columns }

    /// A replay-driving adopted source table.
    ExternalTableSourceStep {
        name,
        kind(coerce = SOURCE_KIND),
        table_name,
        replay_boundary,
        freshness(default = Given::None),
        name_origin(default = Given::Member("SourceNameOrigin", "EXPLICIT"), coerce = SOURCE_NAME_ORIGIN),
    }

    /// A scheduled pull from an external Postgres table, carrying no replay lineage.
    PostgresRefreshSourceStep {
        name,
        kind(coerce = SOURCE_KIND),
        host,
        database,
        table,
        user,
        refresh,
        port(default = Given::Int(5432)),
        password_env(default = Given::None),
        append(default = Given::Bool(true)),
        freshness(default = Given::None),
        name_origin(default = Given::Member("SourceNameOrigin", "EXPLICIT"), coerce = SOURCE_NAME_ORIGIN),
    }

    /// An authored MODEL(...) column declaration.
    ModelColumnSpec {
        name,
        description(default = Given::None),
        audits(default = Given::EmptyTuple),
    }

    /// Adapter query settings scoped by execution phase.
    ExecutionSettings { replay(default = Given::None, coerce = READ_ONLY_MAPPING) }

    /// A transform from one logical upstream node into a managed table.
    TransformStep {
        name,
        source,
        engine,
        order_by,
        relation_name(default = Given::None),
        description(default = Given::None),
        columns(default = Given::EmptyTuple),
        audits(default = Given::EmptyTuple),
        query(default = Given::None),
        sql_file(default = Given::None),
        partition_by(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::None),
        execution_settings(default = Given::Factory(factory::<ExecutionSettings>)),
        replay_anchor(default = Given::Member("ReplayAnchorMode", "AUTO")),
        replay_on_change(default = Given::None),
        bounded_replay_fallback(default = Given::None),
        source_file_path(default = Given::None),
        source_line(default = Given::Int(1)),
        source_column(default = Given::Int(1)),
        retention(default = Given::None),
        on_row_error(default = Given::None),
    }
    @post_init(transform_step)

    /// A query-only terminal model with arbitrary logical upstreams.
    ViewStep {
        name,
        query(default = Given::None),
        sql_file(default = Given::None),
        relation_name(default = Given::None),
        description(default = Given::None),
        columns(default = Given::EmptyTuple),
        audits(default = Given::EmptyTuple),
        source_file_path(default = Given::None),
        source_line(default = Given::Int(1)),
        source_column(default = Given::Int(1)),
    }
    @post_init(view_step)

    /// Effective project values retained by the current compilation model.
    Project {
        replay_on_change(default = Given::None),
        bounded_replay_fallback(default = Given::None, coerce = BOUNDED_REPLAY_FALLBACK),
        model_ttl(default = Given::None),
        default_database(default = Given::None),
        adapter(default = Given::Str("clickhouse")),
        naming(default = Given::Factory(factory::<ProjectNaming>)),
        audit_defaults(default = Given::Factory(factory::<AuditDefaults>)),
        audit_scheduler(default = Given::Factory(factory::<AuditSchedulerConfig>)),
        model_retention(default = Given::None),
        dependencies(default = Given::Factory(factory::<ProjectDependencies>)),
        model_on_row_error(default = Given::None),
    }

    /// A single authored streaming pipeline.
    Pipeline {
        name,
        source,
        transforms(default = Given::Factory(factory::<PyTuple>)),
        mode(default = Given::Member("PipelineMode", "DIRECT")),
        replay_on_change(default = Given::None),
        bounded_replay_fallback(default = Given::None),
        naming(default = Given::Factory(factory::<PipelineNaming>)),
        protection(default = Given::None),
        audit_defaults(default = Given::Factory(factory::<AuditDefaults>)),
        execution_settings(default = Given::Factory(factory::<ExecutionSettings>)),
        model_retention(default = Given::None),
        model_on_row_error(default = Given::None),
    }
    @post_init(pipeline)

    /// A discovered pipeline plus the file it was loaded from.
    LoadedPipeline { pipeline, file_path, project(default = Given::None) }

    /// Parsed project configuration with its retained authored source.
    LoadedProject @no_repr {
        project,
        source_file,
        configuration(default = Given::None),
        effective_configuration(default = Given::None),
        source_files(default = Given::EmptyTuple),
        macro_files(default = Given::EmptyTuple),
        macro_registry(default = Given::None),
    }

    /// One retained standalone source declaration file and its parsed sources.
    DiscoveredSourceFile @no_repr { source_file, sources }

    /// One direct pipeline directory and its optional retained configuration.
    DiscoveredPipelineDirectory @no_repr { pipeline_dir, config_file(default = Given::None) }

    /// All raw project inputs captured before semantic compilation.
    DiscoveredProjectInputs @no_repr {
        project_dir,
        loaded_project,
        source_files,
        pipeline_directories,
        model_files,
        test_files,
        audit_files,
        macro_files,
        access_file(default = Given::None),
        constant_files(default = Given::EmptyTuple),
        enum_files(default = Given::EmptyTuple),
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    add_class::<DiscoveredProjectFile>(module)?;
    add_class::<RawConnectionConfig>(module)?;
    add_class::<AuditSchedulerConfig>(module)?;
    add_class::<AuditSchedulerOverride>(module)?;
    add_class::<SensorAutomationConfig>(module)?;
    add_class::<SensorAutomationOverride>(module)?;
    add_class::<BuildConfig>(module)?;
    add_class::<DestructionConfig>(module)?;
    add_class::<UiConfig>(module)?;
    add_class::<AllowedCrossPipelineReference>(module)?;
    add_class::<ProjectDependencies>(module)?;
    add_class::<ProjectTarget>(module)?;
    add_class::<LocalProjectTarget>(module)?;
    add_class::<ReplayOnChangeRule>(module)?;
    add_class::<ReplayOnChangePolicy>(module)?;
    add_class::<SourceFreshnessPolicy>(module)?;
    add_class::<AuditDefaults>(module)?;
    add_class::<DeploymentReadinessDefaults>(module)?;
    add_class::<KafkaSourceDefaults>(module)?;
    add_class::<ModelRetentionPolicy>(module)?;
    add_class::<KafkaRetentionPolicy>(module)?;
    add_class::<ModelDefaults>(module)?;
    add_class::<SourceDefaults>(module)?;
    add_class::<ProjectDefaults>(module)?;
    add_class::<LocalProjectDefaults>(module)?;
    add_class::<ProjectNaming>(module)?;
    add_class::<PipelineNaming>(module)?;
    add_class::<PipelineProtection>(module)?;
    add_class::<AuthoredProjectConfig>(module)?;
    add_class::<LocalProjectConfig>(module)?;
    add_class::<LoadedProjectConfiguration>(module)?;
    add_class::<EffectiveProjectConfiguration>(module)?;
    add_class::<KafkaLandingStep>(module)?;
    add_class::<ReplayBoundaryColumns>(module)?;
    add_class::<ReplayBoundary>(module)?;
    add_class::<ExternalTableSourceStep>(module)?;
    add_class::<PostgresRefreshSourceStep>(module)?;
    add_class::<ModelColumnSpec>(module)?;
    add_class::<ExecutionSettings>(module)?;
    add_class::<TransformStep>(module)?;
    add_class::<ViewStep>(module)?;
    add_class::<Project>(module)?;
    add_class::<Pipeline>(module)?;
    add_class::<LoadedPipeline>(module)?;
    add_class::<LoadedProject>(module)?;
    add_class::<DiscoveredSourceFile>(module)?;
    add_class::<DiscoveredPipelineDirectory>(module)?;
    add_class::<DiscoveredProjectInputs>(module)?;
    add(
        module,
        "DiscoveryKafkaSettings",
        module.py().get_type::<KafkaSettings>(),
    )
}
