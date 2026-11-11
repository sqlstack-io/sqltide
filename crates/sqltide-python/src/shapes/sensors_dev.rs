//! Internal shapes of the sensor runtime, the event catalog's observation rows and the dev
//! server, plus the sensor value enums.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString, PyTuple};
use sha2::{Digest, Sha256};

use super::{Coerce, Given, add_class, factory, path, shapes, str_enum};

shapes! {
    module = "sqltide.sensors.models";

    /// One registered authored sensor with retained source identity.
    LoadedSensor {
        name,
        kind,
        declaration,
        file_path,
        relative_path,
        source,
        definition_line,
        description(default = Given::None),
        timeout_seconds(default = Given::Float(60.0)),
    }
    @impl {
        /// Fingerprint the sensor identity and retained implementation source.
        #[getter]
        fn identity_fingerprint(&self, py: Python<'_>) -> PyResult<String> {
            // An f-string formats each part with `format(part, "")`, which a `str` subclass may
            // override independently of `__str__`.
            let format: Bound<'_, PyAny> = py.import("builtins")?.getattr("format")?;
            let parts: [Bound<'_, PyAny>; 3] = [
                format.call1((self.name.bind(py),))?,
                format.call1((self.relative_path.bind(py).call_method0("as_posix")?,))?,
                format.call1((self.source.bind(py),))?,
            ];
            let payload: Bound<'_, PyAny> = PyString::new(py, "\0")
                .call_method1("join", (parts,))?
                .call_method1("encode", ("utf-8",))?;
            Ok(Sha256::digest(payload.cast::<PyBytes>()?.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect())
        }

        #[getter]
        fn default_status(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.declaration, &["default_status"])
        }

        #[getter]
        fn retry_policy(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            path(py, &self.declaration, &["retry_policy"])
        }
    }

    /// Compiled project automation: loaded sensors plus discovered providers.
    CompiledSensors { registry, providers(default = Given::EmptyTuple) }

    /// One (completed_at, id) position in an ordered observation stream.
    SensorStreamPosition { completed_at, result_id }

    /// Reduced durable-step state for one (sensor, event, key).
    StepMarker { status, result_json, attempt }

    /// Reduced tick history for one (sensor, event).
    TickAttemptState { failed_attempts, last_failed_at, last_error_message, resolved }

    /// One reduced tick with its terminal state preferred over started rows.
    SensorTickView {
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

    /// Latest polling tick timing and cursor for one sensor.
    PollingTickState { last_started_at, last_success_at, cursor }

    /// Terminal outcome of one isolated sensor handler invocation.
    SensorEvaluation {
        status,
        skip_reason(default = Given::None),
        error_message(default = Given::None),
        cursor(default = Given::None),
    }

    /// Resolution of one matched event delivery within a dispatch pass.
    SensorDeliveryOutcome {
        resolved,
        evaluation(default = Given::None),
        dead_lettered(default = Given::Bool(false)),
    }

    /// Events derived from one positioned observation row.
    SensorEventDelivery { position, events }

    /// Counters describing one dispatch pass.
    SensorDispatchSummary {
        lease_acquired(default = Given::Bool(true)),
        evaluated(default = Given::Int(0)),
        succeeded(default = Given::Int(0)),
        skipped(default = Given::Int(0)),
        failed(default = Given::Int(0)),
        dead_lettered(default = Given::Int(0)),
    }

    /// Deterministic name-keyed registry of loaded project sensors.
    SensorRegistry {
        sensors(default = Given::Factory(factory::<PyDict>), coerce = Coerce::ReadOnlyMapping),
    }
    @impl {
        fn ordered<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
            let sensors: &Bound<'py, PyAny> = self.sensors.bind(py);
            let names: Bound<'py, PyAny> = py
                .import("builtins")?
                .getattr("sorted")?
                .call1((sensors,))?;
            let mut ordered: Vec<Bound<'py, PyAny>> = Vec::new();
            for name in names.try_iter()? {
                ordered.push(sensors.get_item(name?)?);
            }
            PyTuple::new(py, ordered)
        }
    }
}

shapes! {
    module = "sqltide.events.models";

    /// One persisted quality node result row viewed by the event catalog.
    NodeResultObservation {
        result_id,
        invocation_id,
        node_kind,
        node_name,
        binding_key,
        target_identity,
        trigger,
        status,
        severity,
        failure_count,
        completed_at,
        scheduled_for,
        payload_json,
        error_message,
    }

    /// One persisted terminal invocation row viewed by the event catalog.
    InvocationObservation {
        invocation_id,
        command,
        mode,
        outcome,
        exit_code,
        target_identity,
        deployment_id,
        selected_node_count,
        error_message,
        completed_at,
    }
}

shapes! {
    module = "sqltide.dev_server.models";

    /// Approximate progress derived from committed target offset frontiers.
    ReplayOffsetProgress {
        percentage,
        eta_seconds,
        completed_span,
        total_span,
        observed_partitions,
        total_partitions,
    }

    /// Resolved invocation values retained across dev reloads, previews, and builds.
    DevExecutionContext @no_repr {
        database(default = Given::None),
        selected_target(default = Given::None),
        cli_variables(default = Given::EmptyTuple),
        environment(default = Given::None),
        connection_host(default = Given::None),
        connection_port(default = Given::None),
        connection_username(default = Given::None),
        connection_password(default = Given::None),
        run_presumed_failed_after_seconds(default = Given::Int(600)),
        connection_factory(default = Given::None),
        observation_connection_factory(default = Given::None),
    }

    /// A compile failure reduced to what an error page needs.
    CompileErrorInfo {
        message,
        path(default = Given::None),
        line(default = Given::None),
        column(default = Given::None),
        end_line(default = Given::None),
        end_column(default = Given::None),
    }

    /// One held project compile: either servable definitions or the failure.
    CompileOutcome {
        state,
        version_key,
        compiled_at,
        analysis(default = Given::None),
        timings(default = Given::None),
        error(default = Given::None),
    }

    /// Shared control-plane inputs for route-level operation authorization.
    OperationAuthorizationContext { store, project_dir, selected_target }

    /// Durable control-plane stores and their application ownership.
    DevControlStores { accounts, destruction_plans, owns_accounts }
    @impl {
        fn close(&self, py: Python<'_>) -> PyResult<()> {
            self.destruction_plans.bind(py).call_method0("close")?;
            if self.owns_accounts.bind(py).is_truthy()? {
                self.accounts.bind(py).call_method0("close")?;
            }
            Ok(())
        }
    }

    /// Broker metadata for one topic.
    KafkaTopicInfo { name, partition_count, replication_factor, internal }

    /// Best-effort topic inventory for one broker list.
    KafkaTopicsSnapshot { topics }

    /// Broker offsets and lag for one topic partition.
    KafkaPartitionLag { partition, committed_offset, end_offset, lag_messages }

    /// Best-effort consumer-group lag for one managed Kafka source.
    KafkaLagSnapshot { total_messages, partitions }

    /// Row and byte totals for one warehouse relation.
    RelationStorage { rows(default = Given::Int(0)), bytes(default = Given::Int(0)) }

    /// Terminal evidence from one server-owned build cancellation attempt.
    BuildCancellationOutcome {
        invocation_id,
        query_id,
        requested_at,
        status,
        process_exit_code,
        warehouse_supported,
        query_found,
        warehouse_termination_confirmed,
        error_message(default = Given::None),
    }
}

fn enums(module: &Bound<'_, PyModule>) -> PyResult<()> {
    const SENSOR_TYPES: &str = "sqltide.sensors.types";
    str_enum(
        module,
        "SensorKind",
        SENSOR_TYPES,
        "SensorKind",
        Some("Kinds of authored sensors."),
        &[("EVENT", "event"), ("POLLING", "polling")],
    )?;
    str_enum(
        module,
        "DefaultSensorStatus",
        SENSOR_TYPES,
        "DefaultSensorStatus",
        Some("Authored default sensor status declared in code."),
        &[("RUNNING", "running"), ("STOPPED", "stopped")],
    )?;
    str_enum(
        module,
        "SensorOverrideStatus",
        SENSOR_TYPES,
        "SensorOverrideStatus",
        Some("Runtime override state persisted by operator actions."),
        &[
            ("RUNNING", "running"),
            ("STOPPED", "stopped"),
            ("DECLARED_IN_CODE", "declared_in_code"),
        ],
    )?;
    str_enum(
        module,
        "SensorTickStatus",
        SENSOR_TYPES,
        "SensorTickStatus",
        Some("Persisted status of one sensor tick attempt."),
        &[
            ("STARTED", "started"),
            ("SKIPPED", "skipped"),
            ("SUCCEEDED", "succeeded"),
            ("FAILED", "failed"),
            ("DEAD_LETTERED", "dead_lettered"),
        ],
    )?;
    str_enum(
        module,
        "StepPolicy",
        SENSOR_TYPES,
        "StepPolicy",
        Some("Durable step re-execution policy."),
        &[
            ("AT_LEAST_ONCE", "at_least_once"),
            ("AT_MOST_ONCE", "at_most_once"),
        ],
    )
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    enums(module)?;
    add_class::<LoadedSensor>(module)?;
    add_class::<CompiledSensors>(module)?;
    add_class::<SensorStreamPosition>(module)?;
    add_class::<StepMarker>(module)?;
    add_class::<TickAttemptState>(module)?;
    add_class::<SensorTickView>(module)?;
    add_class::<PollingTickState>(module)?;
    add_class::<SensorEvaluation>(module)?;
    add_class::<SensorDeliveryOutcome>(module)?;
    add_class::<SensorEventDelivery>(module)?;
    add_class::<SensorDispatchSummary>(module)?;
    add_class::<SensorRegistry>(module)?;
    add_class::<NodeResultObservation>(module)?;
    add_class::<InvocationObservation>(module)?;
    add_class::<ReplayOffsetProgress>(module)?;
    add_class::<DevExecutionContext>(module)?;
    add_class::<CompileErrorInfo>(module)?;
    add_class::<CompileOutcome>(module)?;
    add_class::<OperationAuthorizationContext>(module)?;
    add_class::<DevControlStores>(module)?;
    add_class::<KafkaTopicInfo>(module)?;
    add_class::<KafkaTopicsSnapshot>(module)?;
    add_class::<KafkaPartitionLag>(module)?;
    add_class::<KafkaLagSnapshot>(module)?;
    add_class::<RelationStorage>(module)?;
    add_class::<BuildCancellationOutcome>(module)
}
