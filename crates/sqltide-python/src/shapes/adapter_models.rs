//! Neutral adapter identity, connection, catalog, metadata and result shapes.

use pyo3::prelude::*;
use pyo3::types::{IntoPyDict, PyDict, PyFrozenSet, PyFrozenSetBuilder, PyString, PyTuple};

use super::{Coerce, Given, add_class, shapes};

const ADAPTER_TYPES: &str = "sqltide.adapter.types";
const ADAPTER_EXCEPTIONS: &str = "sqltide.adapter.exceptions";
const REPLAY_BOUNDARY_MODE: Coerce = Coerce::With(replay_boundary_mode);
const OPTIONAL_STATE_STATUS: Coerce = Coerce::With(optional_state_status);
const HEALTH_AVAILABILITY: Coerce = Coerce::With(health_availability);
const HEALTH_STATUS: Coerce = Coerce::With(health_status);

/// `value` as a member of the adapter enum `name`, which stays defined in Python and is read
/// when first needed so the native module never imports it while it loads.
fn adapter_enum<'py>(value: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import(ADAPTER_TYPES)?
        .getattr(name)?
        .call1((value,))
}

fn replay_boundary_mode<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    adapter_enum(value, "AdapterReplayBoundaryMode")
}

fn optional_state_status<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    adapter_enum(value, "AdapterOptionalStateStatus")
}

fn health_availability<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    adapter_enum(value, "AdapterWarehouseHealthAvailability")
}

fn health_status<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    adapter_enum(value, "AdapterWarehouseHealthStatus")
}

shapes! {
    module = "sqltide.adapter.models";

    /// The registered name of one adapter implementation.
    AdapterIdentity { name }

    /// Capabilities implemented by one adapter.
    AdapterCapabilities {
        virtual_environments,
        managed_source_kinds,
        replay_boundary_modes,
        history_prefix_seed,
        stable_logical_bindings,
        per_relation_atomic_replace,
        graph_atomic_publish,
        set_difference_comparison,
        direct_rebuild,
    }

    /// One retained lineage range required to reproduce a direct target.
    AdapterReplayCoverageRange {
        driving_input_relation_name,
        replay_boundary_mode(coerce = REPLAY_BOUNDARY_MODE),
        boundary_key,
        source_partition_column_name,
        source_position_column_name,
        source_timestamp_column_name,
        lower_value,
        upper_value,
    }

    /// One neutral actual/expected bag comparison requested from an adapter.
    AdapterSetDifferenceTarget { name, column_names, ctes, actual_query, expected_query }

    /// One executable statement containing every SQL-test comparison target.
    AdapterSetDifferenceComparisonRequest { targets }

    /// One column in a neutral adapter resource request.
    AdapterColumn { name, r#type, default_expression(default = Given::None) }

    /// A managed streaming source to realize in a warehouse.
    AdapterManagedSource {
        source_kind,
        name,
        columns,
        broker_list,
        topic,
        consumer_group,
        format,
        settings(default = Given::EmptyTuple),
        naming_macro_fingerprint(default = Given::None),
    }

    /// A managed table to realize in a warehouse.
    AdapterTable {
        name,
        columns,
        engine,
        order_by,
        partition_by(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::EmptyTuple),
    }

    /// A materialized view to realize between warehouse relations.
    AdapterMaterializedView {
        name,
        source_relation_name,
        target_relation_name,
        query,
        database_template,
        refresh(default = Given::None),
        append(default = Given::Bool(true)),
        dead_letters(default = Given::Bool(false)),
    }

    /// An ordinary query view to realize in a warehouse.
    AdapterView { name, query, database_template }

    /// Logical managed-source fields needed for adapter realization.
    AdapterManagedSourceRealizationRequest {
        logical_name,
        source_kind,
        broker_list,
        topic,
        consumer_group,
        format,
        project_name(default = Given::None),
        target_name(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::EmptyTuple),
        naming_macro_fingerprint(default = Given::None),
        consumer_group_identity(default = Given::None),
    }

    /// Logical adopted-source fields needed for adapter realization.
    AdapterAdoptedSourceRealizationRequest { logical_name, relation_name }

    /// One logical source mapped to its adapter relation and resources.
    AdapterSourceRealization { relation_name, resources }

    /// The warehouse-reported refresh state of one scheduled relation.
    AdapterRefreshState {
        view_name,
        status,
        last_refresh_at,
        last_success_at,
        next_refresh_at,
        exception,
    }

    /// One semantically compiled logical model ready for adapter realization.
    AdapterModelRealizationRequest {
        logical_name,
        target_relation_name,
        source_relation_name,
        resolved_query,
        resolved_database_template,
        columns,
        engine,
        order_by,
        partition_by(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::EmptyTuple),
        refresh(default = Given::None),
        append(default = Given::Bool(true)),
        dead_letter_query(default = Given::None),
        dead_letter_database_template(default = Given::None),
    }

    /// One compiled query-only view ready for adapter realization.
    AdapterViewRealizationRequest {
        logical_name,
        target_relation_name,
        resolved_query,
        resolved_database_template,
    }

    /// One logical model mapped to its adapter relation and resources.
    AdapterModelRealization { relation_name, resources }

    /// A stable logical view to bind to one physical relation.
    AdapterStableView { name, target_relation_name }

    /// One logical relation bound to one physical relation.
    AdapterStableBinding { database, logical_name, physical_name }

    /// One obsolete framework-owned stable binding to remove.
    AdapterStableBindingRemoval { database, logical_name }

    /// A set of stable bindings to replace during one publish operation.
    AdapterBindingReplacementRequest { bindings, removals(default = Given::EmptyTuple) }

    /// Physical relations to remove from one database.
    AdapterRelationCleanupRequest { database, relation_names }

    /// Exclusive warehouse-backed ownership of one target mutation slot.
    AdapterTargetMutationLock { database, owner_id }

    /// One staged root whose live and candidate relations must be compared.
    AdapterReadinessRootRequest { database, logical_name, staged_relation_name, active_exists }

    /// A mode-neutral readiness comparison request.
    AdapterReadinessRequest { roots }

    /// Adapter-observed partition and lag metrics for offset replay.
    AdapterReadinessOffsetSummary {
        active_partition_count,
        staged_partition_count,
        partitions_compared,
        missing_staged_partition_count,
        missing_freshness_partition_count,
        lagging_partition_count,
        max_offset_gap,
        average_offset_gap,
        lag_boundary_column,
        max_lag_seconds,
        average_lag_seconds,
    }

    /// Adapter-observed range and lag metrics for scalar replay.
    AdapterReadinessScalarSummary {
        active_min_value,
        active_max_value,
        staged_min_value,
        staged_max_value,
        lag_seconds,
    }

    /// Warehouse observations for one requested staged root.
    AdapterReadinessRootObservation {
        root,
        staged_exists,
        active_row_count,
        staged_row_count,
        replay_source_name,
        replay_source_row_count,
        replay_boundary_mode,
        offset_summary,
        scalar_summary,
    }

    /// A logical relation mapped to its replay-time physical relation.
    AdapterPhysicalRelationMapping { logical_name, physical_name }

    /// A compiler-analyzed query ready for adapter replay realization.
    AdapterReplayQuery { query, physical_relation_mappings, aggregate_semantics }

    /// One inclusive replay cutoff, optionally scoped to a partition.
    AdapterReplayBoundary {
        boundary_key,
        cutoff_value,
        cutoff_inclusive,
        partition_value(default = Given::None),
    }

    /// Physical boundary columns used to read an anchor relation.
    AdapterReplayColumns { partition, offset, timestamp, landed_at, cursor }

    /// The lower-bound policy for one replay root.
    AdapterReplayWindow {
        lower_bound_mode,
        lower_bound_inclusive,
        boundary_time,
        forced_start_time,
        lookback_seconds,
    }

    /// Logical and physical relations participating in one replay root.
    AdapterReplayRelations { root, source, anchor, target }

    /// A mode-neutral request to seed and replay one rebuild root.
    AdapterReplayRequest {
        mode,
        database,
        relations,
        replay_query,
        boundaries,
        columns,
        window,
        seed_mode,
        target_column_names,
        settings(default = Given::EmptyTuple),
        filter_boundaries_at_source(default = Given::Bool(false)),
        dedupe_offsets(default = Given::Bool(false)),
    }

    /// A request for the retained physical ranges selected by one replay window.
    AdapterReplayCoverageRequest {
        replay,
        boundary_column_type,
        count_anchor_rows(default = Given::Bool(false)),
    }

    /// One captured replay lower bound in adapter-neutral string form.
    AdapterReplayLowerBound { value, partition_value(default = Given::None) }

    /// A replay rendered from boundaries captured by the current process.
    AdapterCapturedReplayRequest { replay, boundary_column_type, lower_bounds }

    /// One logical SQL baseline recorded after direct materialization.
    AdapterDirectFingerprintRecord {
        fingerprint_id,
        logical_model_identity,
        definition_sql,
        definition_hash,
        identity_metadata,
        workflow_id,
        tool_version,
        applied_at(default = Given::None),
    }

    /// Explicit availability and latest logical direct SQL baselines.
    AdapterDirectFingerprintSnapshot {
        status(coerce = OPTIONAL_STATE_STATUS),
        baselines,
        warning(default = Given::None),
    }

    /// One physical resource recorded in a published project manifest.
    AdapterManifestResource {
        pipeline_name,
        logical_type,
        logical_name,
        resource_role,
        resource_database,
        resource_name,
        resource_kind,
    }

    /// One complete, immutable publication manifest.
    AdapterManifest {
        manifest_id,
        invocation_id,
        project_identity,
        target_name,
        target_database,
        is_production,
        project_revision,
        manifest_fingerprint,
        manifest_version,
        pipelines,
        resources,
        tool_version,
        published_at,
    }

    /// Availability and complete publication history for one project target.
    AdapterManifestSnapshot {
        status(coerce = OPTIONAL_STATE_STATUS),
        manifests,
        warning(default = Given::None),
    }

    /// A replay whose dynamic boundaries live in deployment watermark metadata.
    AdapterDeploymentReplayRequest {
        replay,
        metadata_database,
        deployment_id,
        boundary_column_type,
        active_relation_name,
        active_column_names,
        anchor_column_names,
    }

    /// A logical object identity stored in framework metadata.
    AdapterMetadataObjectKey { database, object_type, name }

    /// A persisted logical-to-physical object mapping.
    AdapterPreparedObjectMapping { logical_key, physical_name, logical_model_name }

    /// One adapter-neutral object-state metadata record.
    AdapterObjectStateRecord {
        deployment_id,
        key,
        normalized_fingerprint,
        normalized_query,
        recorded_at,
        observation_id(default = Given::Str("")),
        state_kind(default = Given::Str("deployment")),
        physical_database_name(default = Given::None),
        physical_relation_name(default = Given::None),
        logical_model_database(default = Given::None),
        logical_model_name(default = Given::None),
        is_selected_root(default = Given::Bool(false)),
    }

    /// One adapter-neutral staged deployment record.
    AdapterDeploymentRecord {
        deployment_id,
        created_at,
        status,
        replay_lineage_mode,
        selected_root_keys,
        warning_codes,
        prepared_object_mappings,
        workflow_fingerprint(default = Given::Str("")),
        boundary_time(default = Given::None),
        tool_version(default = Given::Str("")),
    }

    /// One adapter-neutral replay watermark record.
    AdapterDeploymentWatermarkRecord {
        deployment_id,
        root_key,
        anchor_key,
        boundary_key,
        cutoff_value,
        lower_value(default = Given::None),
        cutoff_inclusive(default = Given::Bool(true)),
        captured_at(default = Given::Str("1970-01-01 00:00:00.000")),
    }

    /// One adapter-neutral publish-history record.
    AdapterPublishEventRecord {
        deployment_id,
        published_at,
        logical_view_names,
        publication_id(default = Given::Str("")),
        bindings(default = Given::EmptyTuple),
        operation(default = Given::Str("promote")),
        previous_deployment_id(default = Given::None),
    }

    /// One immutable terminal CLI invocation observation.
    AdapterInvocationRecord {
        invocation_id,
        project_identity,
        target_identity,
        command,
        mode,
        outcome,
        exit_code,
        materialized_outcome,
        deployment_id,
        workflow_id,
        selected_node_count,
        started_at,
        completed_at,
        duration_ms,
        error_message,
        summary_json,
        tool_version,
        artifact_project_dir(default = Given::None),
    }

    /// One immutable terminal audit or test result observation.
    AdapterNodeResultRecord {
        result_id,
        invocation_id,
        node_kind,
        node_name,
        binding_key,
        definition_fingerprint,
        execution_fingerprint,
        target_identity,
        trigger,
        scheduled_for,
        cadence_seconds,
        warmup_seconds,
        status,
        severity,
        failure_count,
        completed_at,
        payload_json,
        error_message,
    }

    /// One step-granular workflow event streamed while a run executes.
    AdapterRunEventRecord { invocation_id, sequence, event_kind, step_id, phase, payload_json }

    /// One display-safe warehouse statement durably associated with a run.
    AdapterRunStatementRecord {
        invocation_id,
        statement_sequence,
        step_id,
        phase,
        intent,
        sql,
        sql_sha256,
        workflow_sha256,
        workflow_revision,
    }

    /// One append-only latest-wins sensor stream position.
    AdapterSensorCheckpointRecord { sensor_name, source, position_completed_at, position_result_id }

    /// One persisted sensor evaluation attempt.
    AdapterSensorTickRecord {
        tick_id,
        sensor_name,
        definition_fingerprint,
        kind,
        event_id,
        event_kind,
        attempt,
        status,
        started_at,
        completed_at,
        error_message,
        skip_reason,
        cursor,
    }

    /// One durable step marker keyed by sensor, event, and step key.
    AdapterSensorStepRecord {
        sensor_name,
        event_id,
        step_key,
        policy,
        status,
        attempt,
        result_json,
        error_message,
    }

    /// One append-only operator override of a sensor's declared status.
    AdapterSensorOverrideRecord { override_id, sensor_name, status, actor }

    /// One warehouse-time dispatch lease claim.
    AdapterSensorLeaseRecord { lease_name, owner_id, acquired_at, expires_at }

    /// Sensor state records persisted in one batch.
    AdapterSensorState {
        checkpoints(default = Given::EmptyTuple),
        ticks(default = Given::EmptyTuple),
        steps(default = Given::EmptyTuple),
        overrides(default = Given::EmptyTuple),
        leases(default = Given::EmptyTuple),
    }

    /// One current manifest node joined to persisted terminal history.
    AdapterCurrentQualityNode {
        node_kind,
        node_name,
        binding_key,
        definition_fingerprint,
        execution_fingerprint,
        cadence_seconds(default = Given::None),
        warmup_seconds(default = Given::Int(0)),
    }

    /// Persisted deployments and publish events used for lifecycle cleanup.
    AdapterDeploymentInventory { deployments, publish_events }

    /// A batch of framework metadata records for adapter persistence.
    AdapterMetadataState {
        object_states,
        deployments,
        deployment_watermarks,
        publish_events,
        invocations(default = Given::EmptyTuple),
        node_results(default = Given::EmptyTuple),
    }

    /// Stable identity of one adapter/database catalog observation.
    CatalogIdentity { adapter, database }

    /// One warehouse relation column observed by an adapter.
    CatalogColumn { name, r#type, default_expression(default = Given::None) }

    /// One immutable warehouse relation observation.
    CatalogRelation {
        name,
        engine,
        columns,
        full_engine(default = Given::None),
        order_by(default = Given::EmptyTuple),
        partition_by(default = Given::None),
        ttl(default = Given::None),
        settings(default = Given::EmptyTuple),
        definition_sql(default = Given::None),
        query_sql(default = Given::None),
        source_relation_names(default = Given::EmptyTuple),
        target_relation_name(default = Given::None),
        stable_binding_name(default = Given::None),
        ownership_generation(default = Given::None),
    }
    @impl {
        /// Return the engine including storage arguments when available.
        #[getter]
        fn storage_engine(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let full_engine: &Bound<'_, PyAny> = self.full_engine.bind(py);
            if full_engine.is_truthy()? {
                return Ok(full_engine.clone().unbind());
            }
            Ok(self.engine.clone_ref(py))
        }

        /// Return the first physical source when one exists.
        #[getter]
        fn source_relation_name(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let names: &Bound<'_, PyAny> = self.source_relation_names.bind(py);
            if names.is_truthy()? {
                return Ok(names.get_item(0)?.unbind());
            }
            Ok(py.None())
        }
    }

    /// One immutable point-in-time catalog for an adapter and database.
    CatalogSnapshot { identity, warehouse_timezone, relations }
    @impl {
        /// Return one relation by unqualified name when it exists.
        fn relation(&self, py: Python<'_>, name: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
            for relation in self.relations.bind(py).try_iter()? {
                let relation: Bound<'_, PyAny> = relation?;
                if relation.getattr("name")?.eq(name)? {
                    return Ok(relation.unbind());
                }
            }
            Ok(py.None())
        }

        /// Return every observed unqualified relation name.
        fn relation_names<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyFrozenSet>> {
            let mut names: PyFrozenSetBuilder<'py> = PyFrozenSetBuilder::new(py)?;
            for relation in self.relations.bind(py).try_iter()? {
                names.add(relation?.getattr("name")?)?;
            }
            Ok(names.finalize())
        }
    }

    /// A stable logical view pointing at an active physical table.
    InspectedActiveTableBinding { database, logical_name, physical_name }

    /// A deployment-suffixed physical model candidate for a logical root.
    InspectedPhysicalTableCandidate {
        database,
        logical_name,
        physical_name,
        object_type(default = Given::Str("table")),
    }

    /// Managed table state used for active-deployment resolution.
    InspectedManagedTableState { active_bindings, physical_candidates }

    /// Resolved, format-neutral connection settings for one adapter.
    AdapterConnectionConfig @no_repr {
        host,
        port,
        username,
        password,
        database(default = Given::None),
        settings(default = Given::EmptyTuple),
    }
    @impl {
        /// Render the configuration without exposing the password.
        fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
            let py: Python<'_> = slf.py();
            let config: &Self = slf.get();
            let class: Bound<'_, PyString> = slf.get_type().name()?;
            let host: Bound<'_, PyString> = config.host.bind(py).repr()?;
            let port: Bound<'_, PyString> = config.port.bind(py).repr()?;
            let username: Bound<'_, PyString> = config.username.bind(py).repr()?;
            let password: Bound<'_, PyString> = py
                .import("sqltide._native")?
                .getattr("REDACTED_SECRET_PLACEHOLDER")?
                .repr()?;
            let database: Bound<'_, PyString> = config.database.bind(py).repr()?;
            let settings: Bound<'_, PyString> = config.settings.bind(py).repr()?;
            Ok(format!(
                "{class}(host={host}, port={port}, username={username}, password={password}, \
                 database={database}, settings={settings})"
            ))
        }
    }

    /// A normalized query result returned by the adapter boundary.
    AdapterQueryResult { rows, column_names(default = Given::EmptyTuple) }
    @impl {
        /// Return rows keyed by the query's column names.
        fn named_rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let column_names: &Bound<'py, PyAny> = self.column_names.bind(py);
            let rows: &Bound<'py, PyAny> = self.rows.bind(py);
            if !column_names.is_truthy()? {
                if !rows.is_truthy()? {
                    return Ok(PyTuple::empty(py));
                }
                return Err(PyErr::from_value(
                    py.import(ADAPTER_EXCEPTIONS)?
                        .getattr("AdapterResultError")?
                        .call1(("Query result does not include column names",))?,
                ));
            }
            let zip: Bound<'py, PyAny> = py.import("builtins")?.getattr("zip")?;
            let strict: Bound<'py, PyDict> = [("strict", true)].into_py_dict(py)?;
            let mut named: Vec<Bound<'py, PyAny>> = Vec::new();
            for row in rows.try_iter()? {
                let pairs: Bound<'py, PyAny> = zip.call((column_names, row?), Some(&strict))?;
                named.push(py.get_type::<PyDict>().call1((pairs,))?);
            }
            PyTuple::new(py, named)
        }
    }

    /// Capacity and severity for one configured warehouse disk.
    AdapterWarehouseDisk {
        name,
        path,
        disk_type,
        total_bytes,
        free_bytes,
        unreserved_bytes,
        keep_free_bytes,
        status(coerce = HEALTH_STATUS),
    }

    /// Truthfully labelled warehouse process and execution-boundary memory.
    AdapterWarehouseMemory {
        resident_bytes,
        host_total_bytes,
        cgroup_used_bytes,
        cgroup_limit_bytes,
        basis,
        pressure_fraction,
    }

    /// Current bounded warehouse work counts.
    AdapterWarehouseActivity { active_queries, active_merges, incomplete_mutations }

    /// A Kafka table that keeps failing without committing an offset.
    AdapterWarehouseStuckKafkaConsumer { table, stuck_seconds, recent_errors, latest_error }

    /// Managed Kafka consumer health observed inside the warehouse.
    AdapterWarehouseKafkaConsumers {
        configured_tables,
        materialized_tables,
        materialized_table_names,
        polling_tables,
        exception_tables,
        stuck_consumers(default = Given::EmptyTuple),
    }

    /// Quarantined rows of one model over the last 24 hours and its latest row.
    AdapterWarehouseDeadLetterModel {
        model_name,
        row_count,
        latest_failed_columns,
        latest_payload,
        latest_quarantined_at,
    }

    /// Active physical footprint for one project-database table.
    AdapterWarehouseTable { name, rows, bytes_on_disk, active_parts }

    /// Adapter-neutral point-in-time warehouse diagnostic snapshot.
    AdapterWarehouseHealth {
        availability(coerce = HEALTH_AVAILABILITY),
        status(coerce = HEALTH_STATUS),
        version,
        uptime_seconds,
        disks,
        inode_total,
        inode_free,
        inode_status(coerce = HEALTH_STATUS),
        memory,
        activity,
        tables,
        collection_duration_ms,
        kafka_consumers(default = Given::None),
        dead_letters(default = Given::None),
        capacity_warning_fraction(default = Given::None),
        capacity_critical_fraction(default = Given::None),
        warnings(default = Given::EmptyTuple),
        stale(default = Given::Bool(false)),
        measured_at(default = Given::None),
    }

    /// Warehouse-reported evidence for one executed mutation statement.
    AdapterMutationResult { written_rows(default = Given::None) }

    /// Ephemeral warehouse telemetry for one currently executing statement.
    AdapterStatementProgress {
        elapsed_seconds,
        read_rows,
        read_bytes,
        total_rows_approx,
        memory_usage_bytes,
        settings(default = Given::EmptyTuple),
    }

    /// Adapter-confirmed outcome for one exactly identified workflow query.
    AdapterQueryCancellation {
        query_id,
        supported,
        query_found,
        termination_confirmed,
        detail(default = Given::None),
    }

    /// One captured partition span used only for approximate replay progress.
    AdapterReplayOffsetRange { partition, lower_offset, upper_offset }

    /// Target relation and immutable captured ranges for frontier observation.
    AdapterReplayOffsetProgressRequest {
        database,
        relation,
        partition_column,
        offset_column,
        ranges,
    }

    /// Greatest committed target offset observed for one replay partition.
    AdapterReplayOffsetFrontier { partition, completed_offset }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    add_class::<AdapterIdentity>(module)?;
    add_class::<AdapterCapabilities>(module)?;
    add_class::<AdapterReplayCoverageRange>(module)?;
    add_class::<AdapterSetDifferenceTarget>(module)?;
    add_class::<AdapterSetDifferenceComparisonRequest>(module)?;
    add_class::<AdapterColumn>(module)?;
    add_class::<AdapterManagedSource>(module)?;
    add_class::<AdapterTable>(module)?;
    add_class::<AdapterMaterializedView>(module)?;
    add_class::<AdapterView>(module)?;
    add_class::<AdapterManagedSourceRealizationRequest>(module)?;
    add_class::<AdapterAdoptedSourceRealizationRequest>(module)?;
    add_class::<AdapterSourceRealization>(module)?;
    add_class::<AdapterRefreshState>(module)?;
    add_class::<AdapterModelRealizationRequest>(module)?;
    add_class::<AdapterViewRealizationRequest>(module)?;
    add_class::<AdapterModelRealization>(module)?;
    add_class::<AdapterStableView>(module)?;
    add_class::<AdapterStableBinding>(module)?;
    add_class::<AdapterStableBindingRemoval>(module)?;
    add_class::<AdapterBindingReplacementRequest>(module)?;
    add_class::<AdapterRelationCleanupRequest>(module)?;
    add_class::<AdapterTargetMutationLock>(module)?;
    add_class::<AdapterReadinessRootRequest>(module)?;
    add_class::<AdapterReadinessRequest>(module)?;
    add_class::<AdapterReadinessOffsetSummary>(module)?;
    add_class::<AdapterReadinessScalarSummary>(module)?;
    add_class::<AdapterReadinessRootObservation>(module)?;
    add_class::<AdapterPhysicalRelationMapping>(module)?;
    add_class::<AdapterReplayQuery>(module)?;
    add_class::<AdapterReplayBoundary>(module)?;
    add_class::<AdapterReplayColumns>(module)?;
    add_class::<AdapterReplayWindow>(module)?;
    add_class::<AdapterReplayRelations>(module)?;
    add_class::<AdapterReplayRequest>(module)?;
    add_class::<AdapterReplayCoverageRequest>(module)?;
    add_class::<AdapterReplayLowerBound>(module)?;
    add_class::<AdapterCapturedReplayRequest>(module)?;
    add_class::<AdapterDirectFingerprintRecord>(module)?;
    add_class::<AdapterDirectFingerprintSnapshot>(module)?;
    add_class::<AdapterManifestResource>(module)?;
    add_class::<AdapterManifest>(module)?;
    add_class::<AdapterManifestSnapshot>(module)?;
    add_class::<AdapterDeploymentReplayRequest>(module)?;
    add_class::<AdapterMetadataObjectKey>(module)?;
    add_class::<AdapterPreparedObjectMapping>(module)?;
    add_class::<AdapterObjectStateRecord>(module)?;
    add_class::<AdapterDeploymentRecord>(module)?;
    add_class::<AdapterDeploymentWatermarkRecord>(module)?;
    add_class::<AdapterPublishEventRecord>(module)?;
    add_class::<AdapterInvocationRecord>(module)?;
    add_class::<AdapterNodeResultRecord>(module)?;
    add_class::<AdapterRunEventRecord>(module)?;
    add_class::<AdapterRunStatementRecord>(module)?;
    add_class::<AdapterSensorCheckpointRecord>(module)?;
    add_class::<AdapterSensorTickRecord>(module)?;
    add_class::<AdapterSensorStepRecord>(module)?;
    add_class::<AdapterSensorOverrideRecord>(module)?;
    add_class::<AdapterSensorLeaseRecord>(module)?;
    add_class::<AdapterSensorState>(module)?;
    add_class::<AdapterCurrentQualityNode>(module)?;
    add_class::<AdapterDeploymentInventory>(module)?;
    add_class::<AdapterMetadataState>(module)?;
    add_class::<CatalogIdentity>(module)?;
    add_class::<CatalogColumn>(module)?;
    add_class::<CatalogRelation>(module)?;
    add_class::<CatalogSnapshot>(module)?;
    add_class::<InspectedActiveTableBinding>(module)?;
    add_class::<InspectedPhysicalTableCandidate>(module)?;
    add_class::<InspectedManagedTableState>(module)?;
    add_class::<AdapterConnectionConfig>(module)?;
    add_class::<AdapterQueryResult>(module)?;
    add_class::<AdapterWarehouseDisk>(module)?;
    add_class::<AdapterWarehouseMemory>(module)?;
    add_class::<AdapterWarehouseActivity>(module)?;
    add_class::<AdapterWarehouseStuckKafkaConsumer>(module)?;
    add_class::<AdapterWarehouseKafkaConsumers>(module)?;
    add_class::<AdapterWarehouseDeadLetterModel>(module)?;
    add_class::<AdapterWarehouseTable>(module)?;
    add_class::<AdapterWarehouseHealth>(module)?;
    add_class::<AdapterMutationResult>(module)?;
    add_class::<AdapterStatementProgress>(module)?;
    add_class::<AdapterQueryCancellation>(module)?;
    add_class::<AdapterReplayOffsetRange>(module)?;
    add_class::<AdapterReplayOffsetProgressRequest>(module)?;
    add_class::<AdapterReplayOffsetFrontier>(module)?;
    Ok(())
}
