//! Project and local configuration: decoded TOML in, the discovery dataclasses out.

use std::cell::RefCell;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyMapping, PyString, PyTuple};
use sqltide_discovery::audits::{
    self, HeaderFields, Instance, RenderError, generic_audit_parameter_names as parameter_names,
};
use sqltide_discovery::config::{
    self, AuditDefaults, Build, LocalConfig, Pairs, ProjectConfig, ProjectDefaults, ProjectTarget,
    ReplayOnChange, ReplayRule, RetentionDefault, SensorsOverride, Target,
};
use sqltide_discovery::durations;
use sqltide_discovery::errors::{ErrorKind, SpecError};
use sqltide_discovery::freshness::{self, Freshness};
use sqltide_discovery::header_blocks::{self as test_blocks, Block, LeadingContent};
use sqltide_discovery::pipeline_config::{self, PipelineSettings};
use sqltide_discovery::source_registry::{
    self, Boundary, DerivedName, KafkaDefaults, KafkaRetentionChoice, Port, Source, SourceFile,
    SourceKind,
};

use sqltide_discovery::effective::{self, EffectiveConfig, Invocation};
use sqltide_discovery::interpolation;
use sqltide_discovery::value::index;
use std::collections::HashMap;

use sqltide_discovery::model_spec::{self, ColumnSpec, ModelSpec};

use crate::discovery::PyEnvironment;
use sqltide_discovery::retention::{KafkaRetention, ModelRetention, Retention};
use sqltide_discovery::value::{Kind, Value};

const MODELS_MODULE: &str = "sqltide.compiler.discovery.models";
const TYPES_MODULE: &str = "sqltide.compiler.discovery.types";
const EXCEPTIONS_MODULE: &str = "sqltide.compiler.discovery.exceptions";

/// A decoded document's Python objects, by handle.
struct Document<'py> {
    py: Python<'py>,
    objects: Vec<Bound<'py, PyAny>>,
    models: Bound<'py, PyModule>,
}

impl<'py> Document<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(Self {
            py,
            objects: Vec::new(),
            models: py.import(MODELS_MODULE)?,
        })
    }

    /// Classify like the Python checks: `bool` before `int`, `dict` before other mappings.
    fn value(&mut self, object: &Bound<'py, PyAny>) -> PyResult<Value> {
        let kind: Kind = if object.is_none() {
            Kind::None
        } else if let Ok(flag) = object.downcast::<PyBool>() {
            Kind::Bool(flag.is_true())
        } else if object.is_instance_of::<PyInt>() {
            Kind::Int(object.extract::<i128>().ok(), object.str()?.to_string())
        } else if let Ok(number) = object.downcast::<PyFloat>() {
            Kind::Float(number.value(), object.str()?.to_string())
        } else if let Ok(text) = object.downcast::<PyString>() {
            Kind::Str(text.to_str()?.to_owned())
        } else if object.is_instance_of::<PyList>() {
            Kind::List(self.items(object)?)
        } else if object.is_instance_of::<PyTuple>() {
            Kind::Tuple(self.items(object)?)
        } else if let Ok(dict) = object.downcast::<PyDict>() {
            Kind::Dict(self.entries(dict.as_mapping())?)
        } else if let Ok(mapping) = object.downcast::<PyMapping>() {
            Kind::Mapping(self.entries(mapping)?)
        } else {
            Kind::Other(object.str()?.to_string())
        };
        let text: Option<String> = match kind {
            Kind::List(_) | Kind::Tuple(_) | Kind::Dict(_) | Kind::Mapping(_) => {
                Some(object.str()?.to_string())
            }
            _ => None,
        };
        self.objects.push(object.clone());
        Ok(Value {
            handle: self.objects.len() - 1,
            kind,
            text,
        })
    }

    fn items(&mut self, object: &Bound<'py, PyAny>) -> PyResult<Vec<Value>> {
        object.try_iter()?.map(|item| self.value(&item?)).collect()
    }

    fn entries(&mut self, mapping: &Bound<'py, PyMapping>) -> PyResult<Vec<(Value, Value)>> {
        mapping
            .items()?
            .iter()
            .map(|item| {
                let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item.extract()?;
                Ok((self.value(&key)?, self.value(&value)?))
            })
            .collect()
    }

    fn model(
        &self,
        name: &str,
        fields: &[(&str, Bound<'py, PyAny>)],
    ) -> PyResult<Bound<'py, PyAny>> {
        let kwargs: Bound<'py, PyDict> = PyDict::new(self.py);
        for (key, value) in fields {
            kwargs.set_item(key, value)?;
        }
        self.models.getattr(name)?.call((), Some(&kwargs))
    }

    fn object<T: IntoPyObject<'py>>(&self, value: T) -> PyResult<Bound<'py, PyAny>> {
        value.into_bound_py_any(self.py)
    }

    fn pairs(&self, pairs: &Pairs) -> PyResult<Bound<'py, PyAny>> {
        let items: Vec<Bound<'py, PyTuple>> = pairs
            .iter()
            .map(|(key, handle)| {
                PyTuple::new(self.py, [self.object(key)?, self.objects[*handle].clone()])
            })
            .collect::<PyResult<_>>()?;
        self.object(PyTuple::new(self.py, items)?)
    }

    fn connection(&self, pairs: &Pairs) -> PyResult<Bound<'py, PyAny>> {
        self.model("RawConnectionConfig", &[("values", self.pairs(pairs)?)])
    }

    fn named<T>(
        &self,
        items: &[(String, T)],
        build: impl Fn(&T) -> PyResult<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let items: Vec<Bound<'py, PyTuple>> = items
            .iter()
            .map(|(name, item)| PyTuple::new(self.py, [self.object(name)?, build(item)?]))
            .collect::<PyResult<_>>()?;
        self.object(PyTuple::new(self.py, items)?)
    }

    fn sensors(&self, sensors: &SensorsOverride) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "SensorAutomationOverride",
            &[
                ("enabled", self.object(sensors.enabled)?),
                (
                    "tick_retention_days",
                    self.object(sensors.tick_retention_days)?,
                ),
                (
                    "maximum_event_age_seconds",
                    self.object(sensors.maximum_event_age_seconds)?,
                ),
            ],
        )
    }

    fn build(&self, build: Build) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "BuildConfig",
            &[("max_pipelines", self.object(build.max_pipelines)?)],
        )
    }

    fn target_fields(&self, target: &Target) -> PyResult<Vec<(&'static str, Bound<'py, PyAny>)>> {
        Ok(vec![
            ("database", self.object(target.database.as_deref())?),
            ("connection", self.connection(&target.connection)?),
            ("variables", self.pairs(&target.variables)?),
            (
                "audit_scheduler",
                self.model(
                    "AuditSchedulerOverride",
                    &[("enabled", self.object(target.audit_scheduler_enabled)?)],
                )?,
            ),
            ("sensors", self.sensors(&target.sensors)?),
        ])
    }

    fn project_target(&self, target: &ProjectTarget) -> PyResult<Bound<'py, PyAny>> {
        let mut fields: Vec<(&str, Bound<'py, PyAny>)> = self.target_fields(&target.target)?;
        fields.push(("production", self.object(target.production)?));
        fields.push(("build", self.build(target.build)?));
        fields.push((
            "destruction",
            self.model(
                "DestructionConfig",
                &[(
                    "max_table_size_to_drop_bytes",
                    self.object(target.max_table_size_to_drop_bytes)?,
                )],
            )?,
        ));
        self.model("ProjectTarget", &fields)
    }

    fn project(&self, config: &ProjectConfig) -> PyResult<Bound<'py, PyAny>> {
        let naming: Vec<(&str, Bound<'py, PyAny>)> = [
            ("pipeline_prefix", &config.naming.pipeline_prefix),
            (
                "pipeline_naming_macro",
                &config.naming.pipeline_naming_macro,
            ),
            ("table_prefix", &config.naming.table_prefix),
            ("view_prefix", &config.naming.view_prefix),
        ]
        .into_iter()
        .filter_map(|(key, value)| value.as_deref().map(|text| Ok((key, self.object(text)?))))
        .collect::<PyResult<_>>()?;
        let references: Vec<Bound<'py, PyAny>> = config
            .dependencies
            .allowed_cross_pipeline_references
            .iter()
            .map(|(upstream, downstream)| {
                self.model(
                    "AllowedCrossPipelineReference",
                    &[
                        ("upstream_pipeline", self.object(upstream)?),
                        ("downstream_pipeline", self.object(downstream)?),
                    ],
                )
            })
            .collect::<PyResult<_>>()?;
        self.model(
            "AuthoredProjectConfig",
            &[
                ("name", self.object(&config.name)?),
                ("adapter", self.object(&config.adapter)?),
                ("default_target", self.object(&config.default_target)?),
                ("connection", self.connection(&config.connection)?),
                ("variables", self.pairs(&config.variables)?),
                (
                    "targets",
                    self.named(&config.targets, |target| self.project_target(target))?,
                ),
                ("defaults", self.defaults(&config.defaults)?),
                ("naming", self.model("ProjectNaming", &naming)?),
                (
                    "audit_scheduler",
                    self.model(
                        "AuditSchedulerConfig",
                        &[("enabled", self.object(config.audit_scheduler_enabled)?)],
                    )?,
                ),
                (
                    "sensors",
                    self.model(
                        "SensorAutomationConfig",
                        &[
                            ("enabled", self.object(config.sensors.enabled)?),
                            (
                                "tick_retention_days",
                                self.object(config.sensors.tick_retention_days)?,
                            ),
                            (
                                "maximum_event_age_seconds",
                                self.object(config.sensors.maximum_event_age_seconds)?,
                            ),
                        ],
                    )?,
                ),
                ("build", self.build(config.build)?),
                (
                    "ui",
                    self.model(
                        "UiConfig",
                        &[("timezone", self.object(&config.ui_timezone)?)],
                    )?,
                ),
                (
                    "dependencies",
                    self.model(
                        "ProjectDependencies",
                        &[
                            (
                                "model_reference_scope",
                                self.object(&config.dependencies.model_reference_scope)?,
                            ),
                            (
                                "allowed_cross_pipeline_references",
                                self.object(PyTuple::new(self.py, references)?)?,
                            ),
                        ],
                    )?,
                ),
            ],
        )
    }

    fn local(&self, config: &LocalConfig) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "LocalProjectConfig",
            &[
                ("target", self.object(config.target.as_deref())?),
                ("adapter", self.object(config.adapter.as_deref())?),
                (
                    "defaults",
                    self.model(
                        "LocalProjectDefaults",
                        &[(
                            "pipeline_mode",
                            self.object(config.pipeline_mode.as_deref())?,
                        )],
                    )?,
                ),
                ("connection", self.connection(&config.connection)?),
                ("variables", self.pairs(&config.variables)?),
                (
                    "targets",
                    self.named(&config.targets, |target| {
                        self.model("LocalProjectTarget", &self.target_fields(target)?)
                    })?,
                ),
            ],
        )
    }

    fn replay_rule(&self, rule: Option<ReplayRule>) -> PyResult<Bound<'py, PyAny>> {
        match rule {
            None => self.object(None::<u8>),
            Some(ReplayRule::Full) => {
                self.model("ReplayOnChangeRule", &[("mode", self.object("full")?)])
            }
            Some(ReplayRule::Bounded { lookback_seconds }) => self.model(
                "ReplayOnChangeRule",
                &[
                    ("mode", self.object("bounded")?),
                    ("lookback_seconds", self.object(lookback_seconds)?),
                ],
            ),
        }
    }

    fn replay_on_change(&self, policy: Option<ReplayOnChange>) -> PyResult<Bound<'py, PyAny>> {
        match policy {
            None => self.object(None::<u8>),
            Some(policy) => self.model(
                "ReplayOnChangePolicy",
                &[
                    ("breaking", self.replay_rule(policy.breaking)?),
                    ("non_breaking", self.replay_rule(policy.non_breaking)?),
                ],
            ),
        }
    }

    fn bounded_replay_fallback(&self, fallback: Option<&str>) -> PyResult<Bound<'py, PyAny>> {
        match fallback {
            None => self.object(None::<u8>),
            Some(fallback) => self
                .py
                .import(TYPES_MODULE)?
                .getattr("BoundedReplayFallback")?
                .call1((fallback,)),
        }
    }

    fn freshness(&self, freshness: Option<&Freshness>) -> PyResult<Bound<'py, PyAny>> {
        match freshness {
            None => self.object(None::<u8>),
            Some(freshness) => self.model(
                "SourceFreshnessPolicy",
                &[
                    ("warn_after", self.object(freshness.warn_after.as_deref())?),
                    (
                        "error_after",
                        self.object(freshness.error_after.as_deref())?,
                    ),
                ],
            ),
        }
    }

    fn audits(&self, audits: &AuditDefaults) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "AuditDefaults",
            &[
                ("severity", self.object(audits.severity.as_deref())?),
                ("cadence_seconds", self.object(audits.cadence_seconds)?),
                ("warmup_seconds", self.object(audits.warmup_seconds)?),
            ],
        )
    }

    /// `(retention, retention_template)` for one retention default.
    fn retention<T>(
        &self,
        retention: &RetentionDefault<T>,
        policy: impl Fn(&T) -> PyResult<Bound<'py, PyAny>>,
    ) -> PyResult<[(&'static str, Bound<'py, PyAny>); 2]> {
        let none: Bound<'py, PyAny> = self.object(None::<u8>)?;
        Ok(match retention {
            RetentionDefault::Template(handle) => [
                ("retention", none),
                ("retention_template", self.objects[*handle].clone()),
            ],
            RetentionDefault::Parsed(parsed) => [
                (
                    "retention",
                    match parsed {
                        Retention::Unset => none.clone(),
                        Retention::Disabled => self.object(false)?,
                        Retention::Policy(policy_value) => policy(policy_value)?,
                    },
                ),
                ("retention_template", none),
            ],
        })
    }

    fn kafka_retention(&self, retention: &KafkaRetention) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "KafkaRetentionPolicy",
            &[
                ("duration_seconds", self.object(retention.duration_seconds)?),
                ("timestamp", self.object(&retention.timestamp)?),
                ("fallback", self.object(&retention.fallback)?),
                ("cap_at", self.object(retention.cap_at.as_deref())?),
            ],
        )
    }

    fn model_retention(&self, retention: &ModelRetention) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "ModelRetentionPolicy",
            &[
                ("duration_seconds", self.object(retention.duration_seconds)?),
                (
                    "timestamp_column",
                    self.object(&retention.timestamp_column)?,
                ),
                (
                    "cap_at_column",
                    self.object(retention.cap_at_column.as_deref())?,
                ),
                ("when_missing", self.object(&retention.when_missing)?),
            ],
        )
    }

    fn defaults(&self, defaults: &ProjectDefaults) -> PyResult<Bound<'py, PyAny>> {
        let replay_on_change: Bound<'py, PyAny> =
            self.replay_on_change(defaults.replay_on_change)?;
        let bounded_replay_fallback: Bound<'py, PyAny> =
            self.bounded_replay_fallback(defaults.bounded_replay_fallback.as_deref())?;
        let kafka: [(&str, Bound<'py, PyAny>); 2] = self
            .retention(&defaults.kafka_retention, |policy| {
                self.kafka_retention(policy)
            })?;
        let mut kafka_fields: Vec<(&str, Bound<'py, PyAny>)> = vec![(
            "naming_macro",
            self.object(defaults.kafka_naming_macro.as_deref())?,
        )];
        kafka_fields.extend(kafka);
        let models: [(&str, Bound<'py, PyAny>); 2] = self
            .retention(&defaults.model_retention, |policy| {
                self.model_retention(policy)
            })?;
        self.model(
            "ProjectDefaults",
            &[
                (
                    "managed_source_ttl",
                    self.object(defaults.managed_source_ttl.as_deref())?,
                ),
                ("model_ttl", self.object(defaults.model_ttl.as_deref())?),
                (
                    "kafka_broker_list",
                    self.object(defaults.kafka_broker_list.as_deref())?,
                ),
                ("pipeline_mode", self.object(&defaults.pipeline_mode)?),
                ("replay_on_change", replay_on_change),
                ("bounded_replay_fallback", bounded_replay_fallback),
                (
                    "run_presumed_failed_after_seconds",
                    self.object(defaults.run_presumed_failed_after_seconds)?,
                ),
                ("freshness", self.freshness(defaults.freshness.as_ref())?),
                ("audits", self.audits(&defaults.audits)?),
                (
                    "deployment_readiness",
                    self.model(
                        "DeploymentReadinessDefaults",
                        &[
                            (
                                "maximum_lag_seconds",
                                self.object(defaults.maximum_lag_seconds)?,
                            ),
                            (
                                "minimum_staged_row_ratio",
                                self.object(defaults.minimum_staged_row_ratio)?,
                            ),
                        ],
                    )?,
                ),
                (
                    "sources",
                    self.model(
                        "SourceDefaults",
                        &[("kafka", self.model("KafkaSourceDefaults", &kafka_fields)?)],
                    )?,
                ),
                ("models", self.model("ModelDefaults", &models)?),
            ],
        )
    }
}

/// The configuration error raised as its discovery exception class.
pub(crate) fn spec_error(py: Python<'_>, error: SpecError) -> PyErr {
    let class: &str = match error.kind {
        ErrorKind::ProjectConfig => "ProjectConfigError",
        ErrorKind::DurationParse => "DurationParseError",
        ErrorKind::RetentionConfig => "RetentionConfigError",
        ErrorKind::PipelineDiscovery => "PipelineDiscoveryError",
        ErrorKind::Type => return PyTypeError::new_err(error.message),
    };
    match py
        .import(EXCEPTIONS_MODULE)
        .and_then(|module| module.getattr(class))
        .and_then(|class| class.call1((error.message,)))
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(failure) => failure,
    }
}

/// Validate a decoded project configuration into `AuthoredProjectConfig`; `timezone_exists`
/// reports whether the runtime can load a timezone.
#[pyfunction]
fn parse_project_config<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
    file_path: &str,
    timezone_exists: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut document: Document<'py> = Document::new(py)?;
    let payload: Value = document.value(payload)?;
    let callback_error: RefCell<Option<PyErr>> = RefCell::new(None);
    let mut exists = |name: &str| match timezone_exists
        .call1((name,))
        .and_then(|found| found.is_truthy())
    {
        Ok(found) => found,
        Err(failure) => {
            callback_error.replace(Some(failure));
            false
        }
    };
    let parsed = config::parse_project_config(&payload, file_path, &mut exists);
    if let Some(failure) = callback_error.take() {
        return Err(failure);
    }
    document.project(&parsed.map_err(|error| spec_error(py, error))?)
}

/// Validate a decoded local configuration into `LocalProjectConfig`.
#[pyfunction]
fn parse_local_config<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
    file_path: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let mut document: Document<'py> = Document::new(py)?;
    let payload: Value = document.value(payload)?;
    let parsed: LocalConfig =
        config::parse_local_config(&payload, file_path).map_err(|error| spec_error(py, error))?;
    document.local(&parsed)
}

fn retention_object<'py, T>(
    py: Python<'py>,
    retention: Retention<T>,
    policy: impl Fn(&T) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    match retention {
        Retention::Unset => Ok(py.None().into_bound(py)),
        Retention::Disabled => false.into_bound_py_any(py),
        Retention::Policy(value) => policy(&value),
    }
}

/// Seconds in one `<int><s|m|h|d>` duration.
#[pyfunction]
fn parse_duration_seconds(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    field_path: &str,
    allow_zero: bool,
) -> PyResult<u64> {
    let text: Option<String> = value
        .downcast::<PyString>()
        .ok()
        .map(|text| text.to_string());
    durations::parse_duration_seconds(text.as_deref(), field_path, allow_zero)
        .map_err(|error| spec_error(py, error))
}

/// An optional freshness mapping as a `SourceFreshnessPolicy`, or `None`.
#[pyfunction]
fn parse_freshness_policy<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
    label: &str,
    file_path: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let mut document: Document<'py> = Document::new(py)?;
    let payload: Value = document.value(payload)?;
    let parsed: Option<Freshness> =
        freshness::parse_freshness_policy(Some(&payload), label, file_path)
            .map_err(|error| spec_error(py, error))?;
    document.freshness(parsed.as_ref())
}

/// Check `pipeline.toml`'s keys and return its mode, `default_mode` when unset.
#[pyfunction]
fn parse_pipeline_mode(
    py: Python<'_>,
    payload: &Bound<'_, PyAny>,
    file_path: &str,
    default_mode: &str,
) -> PyResult<String> {
    let mut document: Document<'_> = Document::new(py)?;
    let payload: Value = document.value(payload)?;
    pipeline_config::parse_pipeline_mode(&payload, file_path, default_mode)
        .map_err(|error| spec_error(py, error))
}

/// `pipeline.toml`'s other settings: `(replay_on_change, bounded_replay_fallback, naming,
/// protection, audit_defaults, execution_settings, model_retention)`.
#[pyfunction]
fn parse_pipeline_settings<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
    file_path: &str,
    pipeline_name: &str,
    mode: &str,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut document: Document<'py> = Document::new(py)?;
    let payload: Value = document.value(payload)?;
    let settings: PipelineSettings =
        pipeline_config::parse_pipeline_settings(&payload, file_path, pipeline_name, mode)
            .map_err(|error| spec_error(py, error))?;
    let protection: Bound<'py, PyAny> = match &settings.protection {
        None => document.object(None::<u8>)?,
        Some((warning, confirmation)) => document.model(
            "PipelineProtection",
            &[
                ("warning", document.object(warning)?),
                ("confirmation", document.object(confirmation)?),
            ],
        )?,
    };
    let replay: Bound<'py, PyAny> = match &settings.replay_settings {
        None => document.object(None::<u8>)?,
        Some(pairs) => {
            let replay: Bound<'py, PyDict> = PyDict::new(py);
            for (name, value) in pairs {
                replay.set_item(name, value)?;
            }
            replay.into_any()
        }
    };
    PyTuple::new(
        py,
        [
            document.replay_on_change(settings.replay_on_change)?,
            document.bounded_replay_fallback(settings.bounded_replay_fallback.as_deref())?,
            document.model(
                "PipelineNaming",
                &[
                    (
                        "table_prefix",
                        document.object(settings.table_prefix.as_deref())?,
                    ),
                    (
                        "view_prefix",
                        document.object(settings.view_prefix.as_deref())?,
                    ),
                ],
            )?,
            protection,
            document.audits(&settings.audit_defaults)?,
            document.model("ExecutionSettings", &[("replay", replay)])?,
            retention_object(py, settings.model_retention, |policy| {
                document.model_retention(policy)
            })?,
        ],
    )
}

impl<'py> Document<'py> {
    fn boundary(&self, boundary: &Boundary) -> PyResult<Bound<'py, PyAny>> {
        let [partition, offset, timestamp, landed_at, cursor] = &boundary.columns.0;
        self.model(
            "ReplayBoundary",
            &[
                ("mode", self.object(&boundary.mode)?),
                (
                    "columns",
                    self.model(
                        "ReplayBoundaryColumns",
                        &[
                            ("partition", self.object(partition.as_deref())?),
                            ("offset", self.object(offset.as_deref())?),
                            ("timestamp", self.object(timestamp.as_deref())?),
                            ("landed_at", self.object(landed_at.as_deref())?),
                            ("cursor", self.object(cursor.as_deref())?),
                        ],
                    )?,
                ),
            ],
        )
    }

    fn source(
        &self,
        source: &Source,
        default_retention: &Bound<'py, PyAny>,
        default_freshness: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let freshness: Bound<'py, PyAny> = match &source.freshness {
            Some(_) => self.freshness(source.freshness.as_ref())?,
            None => default_freshness.clone(),
        };
        match &source.kind {
            SourceKind::Kafka(kafka) => {
                let (retention, origin): (Bound<'py, PyAny>, Option<&str>) = match &kafka.retention
                {
                    KafkaRetentionChoice::Source(retention) => (
                        retention_object(self.py, retention.clone(), |policy| {
                            self.kafka_retention(policy)
                        })?,
                        Some("source"),
                    ),
                    KafkaRetentionChoice::Project => (default_retention.clone(), Some("project")),
                    KafkaRetentionChoice::None => (self.object(None::<u8>)?, None),
                };
                let settings: Bound<'py, PyAny> = if kafka.settings.is_empty() {
                    self.object(None::<u8>)?
                } else {
                    let settings: Bound<'py, PyDict> = PyDict::new(self.py);
                    for (key, value) in &kafka.settings {
                        settings.set_item(key, value)?;
                    }
                    settings.into_any()
                };
                let (macro_name, fingerprint) = match &kafka.derived {
                    Some((macro_name, fingerprint)) => {
                        (Some(macro_name.as_str()), Some(fingerprint.as_str()))
                    }
                    None => (None, None),
                };
                self.model(
                    "KafkaLandingStep",
                    &[
                        ("name", self.object(&kafka.name)?),
                        (
                            "kafka",
                            self.model(
                                "KafkaSettings",
                                &[
                                    ("broker_list", self.object(&kafka.broker_list)?),
                                    ("topic", self.object(&kafka.topic)?),
                                    (
                                        "consumer_group",
                                        self.object(kafka.consumer_group.as_deref())?,
                                    ),
                                    ("format", self.object(&kafka.format)?),
                                    ("ttl", self.object(kafka.ttl.as_deref())?),
                                    ("settings", settings),
                                    ("retention", retention),
                                    ("retention_origin", self.object(origin)?),
                                ],
                            )?,
                        ),
                        ("replay_boundary", self.boundary(&kafka.replay_boundary)?),
                        ("freshness", freshness),
                        (
                            "name_origin",
                            self.object(if kafka.derived.is_some() {
                                "derived"
                            } else {
                                "explicit"
                            })?,
                        ),
                        ("naming_macro", self.object(macro_name)?),
                        ("naming_macro_fingerprint", self.object(fingerprint)?),
                    ],
                )
            }
            SourceKind::Adopted {
                name,
                table_name,
                replay_boundary,
            } => self.model(
                "ExternalTableSourceStep",
                &[
                    ("name", self.object(name)?),
                    ("kind", self.object("stream_table")?),
                    ("table_name", self.object(table_name)?),
                    ("replay_boundary", self.boundary(replay_boundary)?),
                    ("freshness", freshness),
                ],
            ),
            SourceKind::Postgres(postgres) => self.model(
                "PostgresRefreshSourceStep",
                &[
                    ("name", self.object(&postgres.name)?),
                    ("kind", self.object("postgres")?),
                    ("host", self.object(&postgres.host)?),
                    ("database", self.object(&postgres.database)?),
                    ("table", self.object(&postgres.table)?),
                    ("user", self.object(&postgres.user)?),
                    ("refresh", self.object(&postgres.refresh)?),
                    (
                        "port",
                        match postgres.port {
                            Port::Number(number) => self.object(number)?,
                            Port::Handle(handle) => self.objects[handle].clone(),
                        },
                    ),
                    (
                        "password_env",
                        self.object(postgres.password_env.as_deref())?,
                    ),
                    ("append", self.object(postgres.append)?),
                    ("freshness", freshness),
                ],
            ),
        }
    }
}

impl<'py> Document<'py> {
    fn interpolated(&self, value: &interpolation::Value) -> PyResult<Bound<'py, PyAny>> {
        Ok(match value {
            interpolation::Value::Str(text) => self.object(text)?,
            interpolation::Value::Seq(items) => self.object(PyTuple::new(
                self.py,
                items
                    .iter()
                    .map(|item| self.interpolated(item))
                    .collect::<PyResult<Vec<_>>>()?,
            )?)?,
            interpolation::Value::Map(entries) => {
                let dict: Bound<'py, PyDict> = PyDict::new(self.py);
                for (key, item) in entries {
                    dict.set_item(key, self.interpolated(item)?)?;
                }
                dict.into_any()
            }
            interpolation::Value::Other { handle, .. } => self.objects[*handle].clone(),
        })
    }

    fn effective(&self, config: &EffectiveConfig) -> PyResult<Bound<'py, PyAny>> {
        let variables: Vec<Bound<'py, PyTuple>> = config
            .variables
            .iter()
            .map(|(name, value)| {
                PyTuple::new(self.py, [self.object(name)?, self.interpolated(value)?])
            })
            .collect::<PyResult<_>>()?;
        let references: Vec<Bound<'py, PyAny>> = config
            .dependencies
            .allowed_cross_pipeline_references
            .iter()
            .map(|(upstream, downstream)| {
                self.model(
                    "AllowedCrossPipelineReference",
                    &[
                        ("upstream_pipeline", self.object(upstream)?),
                        ("downstream_pipeline", self.object(downstream)?),
                    ],
                )
            })
            .collect::<PyResult<_>>()?;
        self.model(
            "EffectiveProjectConfiguration",
            &[
                ("name", self.object(&config.name)?),
                ("adapter", self.object(&config.adapter)?),
                ("target_name", self.object(&config.target_name)?),
                ("database", self.object(config.database.as_deref())?),
                ("connection", self.connection(&config.connection)?),
                ("variables", self.object(PyTuple::new(self.py, variables)?)?),
                ("defaults", self.defaults(&config.defaults)?),
                (
                    "naming",
                    self.model(
                        "ProjectNaming",
                        &[
                            (
                                "pipeline_prefix",
                                self.object(&config.naming.pipeline_prefix)?,
                            ),
                            (
                                "pipeline_naming_macro",
                                self.object(config.naming.pipeline_naming_macro.as_deref())?,
                            ),
                            ("table_prefix", self.object(&config.naming.table_prefix)?),
                            ("view_prefix", self.object(&config.naming.view_prefix)?),
                        ],
                    )?,
                ),
                (
                    "audit_scheduler",
                    self.model(
                        "AuditSchedulerConfig",
                        &[("enabled", self.object(config.audit_scheduler_enabled)?)],
                    )?,
                ),
                (
                    "sensors",
                    self.model(
                        "SensorAutomationConfig",
                        &[
                            ("enabled", self.object(config.sensors.enabled)?),
                            (
                                "tick_retention_days",
                                self.object(config.sensors.tick_retention_days)?,
                            ),
                            (
                                "maximum_event_age_seconds",
                                self.object(config.sensors.maximum_event_age_seconds)?,
                            ),
                        ],
                    )?,
                ),
                ("build", self.build(config.build)?),
                (
                    "destruction",
                    self.model(
                        "DestructionConfig",
                        &[(
                            "max_table_size_to_drop_bytes",
                            self.object(config.max_table_size_to_drop_bytes)?,
                        )],
                    )?,
                ),
                (
                    "ui",
                    self.model(
                        "UiConfig",
                        &[("timezone", self.object(&config.ui_timezone)?)],
                    )?,
                ),
                ("production_target", self.object(config.production_target)?),
                (
                    "dependencies",
                    self.model(
                        "ProjectDependencies",
                        &[
                            (
                                "model_reference_scope",
                                self.object(&config.dependencies.model_reference_scope)?,
                            ),
                            (
                                "allowed_cross_pipeline_references",
                                self.object(PyTuple::new(self.py, references)?)?,
                            ),
                        ],
                    )?,
                ),
            ],
        )
    }
}

/// Resolve the effective configuration for a target from the decoded project and optional
/// local configuration, command-line variables, and the environment.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn resolve_effective_configuration<'py>(
    py: Python<'py>,
    project_payload: &Bound<'py, PyAny>,
    project_path: &str,
    local_payload: &Bound<'py, PyAny>,
    local_path: &str,
    selected_target: Option<&str>,
    cli_variables: &Bound<'py, PyAny>,
    environment: Bound<'py, PyAny>,
    timezone_exists: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut document: Document<'py> = Document::new(py)?;
    let project_payload: Value = document.value(project_payload)?;
    let local_payload: Value = if local_payload.is_none() {
        document.value(PyDict::new(py).as_any())?
    } else {
        document.value(local_payload)?
    };
    let cli_variables: Value = document.value(cli_variables)?;
    let callback_error: RefCell<Option<PyErr>> = RefCell::new(None);
    let mut exists = |name: &str| match timezone_exists
        .call1((name,))
        .and_then(|found| found.is_truthy())
    {
        Ok(found) => found,
        Err(failure) => {
            callback_error.replace(Some(failure));
            false
        }
    };
    let project_config = config::parse_project_config(&project_payload, project_path, &mut exists);
    if let Some(failure) = callback_error.take() {
        return Err(failure);
    }
    let project_config: ProjectConfig = project_config.map_err(|error| spec_error(py, error))?;
    let local: LocalConfig = config::parse_local_config(&local_payload, local_path)
        .map_err(|error| spec_error(py, error))?;
    let mut objects: HashMap<usize, &Value> = HashMap::new();
    index(&project_payload, &mut objects);
    index(&local_payload, &mut objects);
    index(&cli_variables, &mut objects);
    let cli: Vec<(String, &Value)> = cli_variables
        .mapping_entries()
        .unwrap_or_default()
        .iter()
        .map(|(key, value)| (key.python_str(), value))
        .collect();
    let resolved: EffectiveConfig = effective::resolve_effective_config(
        &project_config,
        &local,
        &objects,
        &Invocation {
            selected_target,
            cli_variables: &cli,
            environment: &PyEnvironment(environment),
            project_path,
        },
    )
    .map_err(|error| spec_error(py, error))?;
    document.effective(&resolved)
}

impl<'py> Document<'py> {
    fn handles(&self, handles: &[usize]) -> PyResult<Bound<'py, PyAny>> {
        self.object(PyTuple::new(
            self.py,
            handles.iter().map(|handle| self.objects[*handle].clone()),
        )?)
    }

    fn pairs_dict(&self, pairs: &[(String, String)]) -> PyResult<Bound<'py, PyAny>> {
        let dict: Bound<'py, PyDict> = PyDict::new(self.py);
        for (key, value) in pairs {
            dict.set_item(key, value)?;
        }
        Ok(dict.into_any())
    }

    fn column(&self, column: &ColumnSpec) -> PyResult<Bound<'py, PyAny>> {
        self.model(
            "ModelColumnSpec",
            &[
                ("name", self.object(&column.name)?),
                ("description", self.object(column.description.as_deref())?),
                ("audits", self.handles(&column.audits)?),
            ],
        )
    }
}

/// Reject `MODEL(...)` header keys outside the model schema.
#[pyfunction]
fn check_model_header_keys(
    py: Python<'_>,
    header: &Bound<'_, PyAny>,
    file_path: &str,
) -> PyResult<()> {
    let mut document: Document<'_> = Document::new(py)?;
    let header: Value = document.value(header)?;
    model_spec::check_model_header_keys(&header, file_path).map_err(|error| spec_error(py, error))
}

/// Validate a parsed `MODEL(...)` header into `(kind, fields)`, the keyword arguments of a
/// `ViewStep` or `TransformStep` besides its name, query, and position. `check_query(kind=)`
/// validates a view's references or returns a table's driving input.
#[pyfunction]
fn parse_model_spec<'py>(
    py: Python<'py>,
    header: &Bound<'py, PyAny>,
    file_path: &str,
    check_query: &Bound<'py, PyAny>,
) -> PyResult<(String, Bound<'py, PyDict>)> {
    let mut document: Document<'py> = Document::new(py)?;
    let header: Value = document.value(header)?;
    let callback_error: RefCell<Option<PyErr>> = RefCell::new(None);
    let mut check = |kind: &str| -> Result<Option<String>, SpecError> {
        let kwargs: Bound<'py, PyDict> = PyDict::new(py);
        kwargs
            .set_item("kind", kind)
            .and_then(|()| check_query.call((), Some(&kwargs)))
            .and_then(|result| result.extract::<Option<String>>())
            .map_err(|failure| {
                callback_error.replace(Some(failure));
                SpecError::new(ErrorKind::PipelineDiscovery, String::new())
            })
    };
    let spec = model_spec::model_spec(&header, file_path, &mut check);
    if let Some(failure) = callback_error.take() {
        return Err(failure);
    }
    let spec: ModelSpec = spec.map_err(|error| spec_error(py, error))?;
    let (common, table) = match &spec {
        ModelSpec::View(common) => (common, None),
        ModelSpec::Table(common, table) => (common, Some(table)),
    };
    let fields: Bound<'py, PyDict> = PyDict::new(py);
    fields.set_item("relation_name", common.relation_name.as_deref())?;
    fields.set_item("description", common.description.as_deref())?;
    fields.set_item(
        "columns",
        PyTuple::new(
            py,
            common
                .columns
                .iter()
                .map(|column| document.column(column))
                .collect::<PyResult<Vec<_>>>()?,
        )?,
    )?;
    fields.set_item("audits", document.handles(&common.audits)?)?;
    let Some(table) = table else {
        return Ok(("view".to_owned(), fields));
    };
    fields.set_item("source", &table.source)?;
    fields.set_item("engine", &table.engine)?;
    fields.set_item("order_by", PyList::new(py, &table.order_by)?)?;
    fields.set_item("partition_by", table.partition_by.as_deref())?;
    fields.set_item("ttl", table.ttl.as_deref())?;
    fields.set_item(
        "retention",
        retention_object(py, table.retention.clone(), |policy| {
            document.model_retention(policy)
        })?,
    )?;
    fields.set_item(
        "settings",
        match &table.settings {
            None => document.object(None::<u8>)?,
            Some(settings) => document.pairs_dict(settings)?,
        },
    )?;
    fields.set_item(
        "execution_settings",
        document.model(
            "ExecutionSettings",
            &[(
                "replay",
                match &table.replay_settings {
                    None => document.object(None::<u8>)?,
                    Some(settings) => document.pairs_dict(settings)?,
                },
            )],
        )?,
    )?;
    fields.set_item("replay_anchor", &table.replay_anchor)?;
    fields.set_item(
        "replay_on_change",
        document.replay_on_change(table.replay_on_change)?,
    )?;
    fields.set_item(
        "bounded_replay_fallback",
        table.bounded_replay_fallback.as_deref(),
    )?;
    Ok(("table".to_owned(), fields))
}

/// Parse `[(path, payload, decode_error)]` source files in order into one tuple of sources
/// per file. `derive_name(topic=, label=, file_path=)` runs the Kafka naming macro and returns
/// `(name, macro_name, fingerprint)`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn parse_source_registry<'py>(
    py: Python<'py>,
    files: Vec<(String, Bound<'py, PyAny>, Option<String>)>,
    variables: &Bound<'py, PyAny>,
    environment: Bound<'py, PyAny>,
    default_ttl: Option<String>,
    default_retention: Bound<'py, PyAny>,
    default_broker_list: Option<String>,
    default_freshness: Bound<'py, PyAny>,
    derive_name: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyTuple>>> {
    let mut document: Document<'py> = Document::new(py)?;
    let variables: Value = document.value(variables)?;
    let source_files: Vec<SourceFile> = files
        .iter()
        .map(|(path, payload, decode_error)| {
            Ok(SourceFile {
                path: path.clone(),
                payload: match decode_error {
                    Some(message) => Err(message.clone()),
                    None => Ok(document.value(payload)?),
                },
            })
        })
        .collect::<PyResult<_>>()?;
    let defaults: KafkaDefaults = KafkaDefaults {
        ttl: default_ttl,
        retention_set: !default_retention.is_none(),
        broker_list: default_broker_list,
    };
    let callback_error: RefCell<Option<PyErr>> = RefCell::new(None);
    let mut derive =
        |topic: &str, label: &str, file_path: &str| -> Result<DerivedName, SpecError> {
            let kwargs: Bound<'py, PyDict> = PyDict::new(py);
            kwargs
                .set_item("topic", topic)
                .and_then(|()| kwargs.set_item("label", label))
                .and_then(|()| kwargs.set_item("file_path", file_path))
                .and_then(|()| derive_name.call((), Some(&kwargs)))
                .and_then(|result| result.extract::<DerivedName>())
                .map_err(|failure| {
                    callback_error.replace(Some(failure));
                    SpecError::new(ErrorKind::PipelineDiscovery, String::new())
                })
        };
    let parsed = source_registry::parse_source_registry(
        &source_files,
        &variables,
        &PyEnvironment(environment),
        &defaults,
        &mut derive,
    );
    if let Some(failure) = callback_error.take() {
        return Err(failure);
    }
    parsed
        .map_err(|error| spec_error(py, error))?
        .iter()
        .map(|sources| {
            PyTuple::new(
                py,
                sources
                    .iter()
                    .map(|source| document.source(source, &default_retention, &default_freshness))
                    .collect::<PyResult<Vec<_>>>()?,
            )
        })
        .collect()
}

/// `(start, (header, sql, sql start) | None)` of one SQL test block, with character offsets.
type SqlTestBlockResult = (usize, Option<(String, String, usize)>);

/// The `TEST(...);` blocks of one SQL test file. Raises `ValueError` when content precedes the
/// first header.
#[pyfunction]
fn sql_test_blocks(contents: &str) -> PyResult<Vec<SqlTestBlockResult>> {
    let blocks: Vec<Block> = test_blocks::sql_test_blocks(contents)
        .map_err(|LeadingContent| PyValueError::new_err("leading content"))?;
    Ok(blocks
        .into_iter()
        .map(|block| {
            (
                block.start,
                block
                    .header
                    .map(|header| (header.header, header.sql, header.sql_start)),
            )
        })
        .collect())
}

/// `(header, sql, query line, query column)` of a model file's leading `MODEL(...);` header,
/// or `None`.
#[pyfunction]
fn model_header_block(contents: &str) -> Option<(String, String, usize, usize)> {
    test_blocks::model_header_block(contents)
}

/// `(name, mode)` of one parsed `TEST()` header. Raises `ValueError(message)` for unsupported
/// keys, then an invalid name, then an invalid mode.
#[pyfunction]
fn sql_test_header_fields(
    py: Python<'_>,
    file_path: &str,
    header: Vec<(String, Bound<'_, PyAny>)>,
) -> PyResult<(Option<String>, String)> {
    let mut document: Document<'_> = Document::new(py)?;
    let header: Vec<(String, Value)> = header
        .iter()
        .map(|(key, object)| Ok((key.clone(), document.value(object)?)))
        .collect::<PyResult<_>>()?;
    let header: Vec<(String, &Value)> = header
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect();
    if let Some(message) = test_blocks::unsupported_test_header_keys(file_path, &header) {
        return Err(PyValueError::new_err(message));
    }
    test_blocks::test_header_fields(file_path, &header).map_err(PyValueError::new_err)
}

/// `(severity, description, name, severity is explicit, every seconds, warmup seconds,
/// scheduled)` of one concrete `AUDIT()` header.
type AuditHeaderResult = (
    String,
    Option<String>,
    Option<String>,
    bool,
    Option<u64>,
    Option<u64>,
    Option<bool>,
);

/// The message naming a parsed `AUDIT()` header's unsupported keys, or `None`.
#[pyfunction]
fn unsupported_audit_header_keys(
    py: Python<'_>,
    file_path: &str,
    header: Vec<(String, Bound<'_, PyAny>)>,
) -> PyResult<Option<String>> {
    let mut document: Document<'_> = Document::new(py)?;
    let header: Vec<(String, Value)> = header
        .iter()
        .map(|(key, object)| Ok((key.clone(), document.value(object)?)))
        .collect::<PyResult<_>>()?;
    let header: Vec<(String, &Value)> = header
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect();
    Ok(audits::unsupported_audit_header_keys(file_path, &header))
}

/// The settings of one parsed `AUDIT()` header. Raises `ValueError(message)` for the first
/// invalid setting.
#[pyfunction]
fn audit_header_fields(
    py: Python<'_>,
    file_path: &str,
    header: Vec<(String, Bound<'_, PyAny>)>,
) -> PyResult<AuditHeaderResult> {
    let mut document: Document<'_> = Document::new(py)?;
    let header: Vec<(String, Value)> = header
        .iter()
        .map(|(key, object)| Ok((key.clone(), document.value(object)?)))
        .collect::<PyResult<_>>()?;
    let header: Vec<(String, &Value)> = header
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect();
    let fields: HeaderFields =
        audits::audit_header_fields(file_path, &header).map_err(PyValueError::new_err)?;
    Ok((
        fields.severity,
        fields.description,
        fields.name,
        fields.severity_is_explicit,
        fields.cadence_seconds,
        fields.warmup_seconds,
        fields.scheduled,
    ))
}

/// `(definition name, (key, object) arguments, name, severity, description, severity is
/// explicit, every seconds, warmup seconds, scheduled)` of one generic audit instance.
type GenericAuditInstanceResult<'py> = (
    String,
    Vec<(String, Bound<'py, PyAny>)>,
    String,
    String,
    Option<String>,
    bool,
    Option<u64>,
    Option<u64>,
    Option<bool>,
);

/// The generic audits one model or column header declares. Raises `ValueError(message)` for
/// the first invalid entry.
#[pyfunction]
fn generic_audit_instances<'py>(
    py: Python<'py>,
    file_path: &str,
    raw_audits: &Bound<'py, PyAny>,
    implicit_arguments: Vec<(String, Bound<'py, PyAny>)>,
    default_name_prefix: &str,
) -> PyResult<Vec<GenericAuditInstanceResult<'py>>> {
    let mut document: Document<'py> = Document::new(py)?;
    let raw_audits: Value = document.value(raw_audits)?;
    let implicit: Vec<(String, Value)> = implicit_arguments
        .iter()
        .map(|(key, object)| Ok((key.clone(), document.value(object)?)))
        .collect::<PyResult<_>>()?;
    let implicit: Vec<(String, &Value)> = implicit
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect();
    let instances: Vec<Instance<'_>> =
        audits::generic_audit_instances(file_path, &raw_audits, &implicit, default_name_prefix)
            .map_err(PyValueError::new_err)?;
    Ok(instances
        .into_iter()
        .map(|instance| {
            (
                instance.definition_name,
                instance
                    .arguments
                    .into_iter()
                    .map(|(key, value)| (key, document.objects[value.handle].clone()))
                    .collect(),
                instance.name,
                instance.severity,
                instance.description,
                instance.severity_is_explicit,
                instance.cadence_seconds,
                instance.warmup_seconds,
                instance.scheduled,
            )
        })
        .collect())
}

/// `(raw parameter names, quoted parameter names)` of a generic audit definition query.
#[pyfunction]
fn generic_audit_parameter_names(query: &str) -> (Vec<String>, Vec<String>) {
    parameter_names(query)
}

/// The concrete query of one generic audit instance. Raises `ValueError(("invalid", message))`
/// or `ValueError(("literal", text))` for a string the dialect could not render.
#[pyfunction]
fn render_generic_audit<'py>(
    py: Python<'py>,
    file_path: &str,
    definition_name: &str,
    query: &str,
    raw_parameter_names: Vec<String>,
    quoted_parameter_names: Vec<String>,
    arguments: Vec<(String, Bound<'py, PyAny>)>,
) -> PyResult<String> {
    let mut document: Document<'py> = Document::new(py)?;
    let arguments: Vec<(String, Value)> = arguments
        .iter()
        .map(|(key, object)| Ok((key.clone(), document.value(object)?)))
        .collect::<PyResult<_>>()?;
    let arguments: Vec<(String, &Value)> = arguments
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect();
    audits::render_generic_audit(
        file_path,
        definition_name,
        query,
        &raw_parameter_names,
        &quoted_parameter_names,
        &arguments,
    )
    .map_err(|error| match error {
        RenderError::Invalid(message) => PyValueError::new_err(("invalid", message)),
        RenderError::Literal(text) => PyValueError::new_err(("literal", text)),
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(sql_test_blocks, module)?)?;
    module.add_function(wrap_pyfunction!(model_header_block, module)?)?;
    module.add_function(wrap_pyfunction!(sql_test_header_fields, module)?)?;
    module.add_function(wrap_pyfunction!(unsupported_audit_header_keys, module)?)?;
    module.add_function(wrap_pyfunction!(audit_header_fields, module)?)?;
    module.add_function(wrap_pyfunction!(generic_audit_instances, module)?)?;
    module.add_function(wrap_pyfunction!(generic_audit_parameter_names, module)?)?;
    module.add_function(wrap_pyfunction!(render_generic_audit, module)?)?;
    module.add("DEFAULT_ADAPTER_NAME", config::DEFAULT_ADAPTER_NAME)?;
    module.add("DEFAULT_PIPELINE_PREFIX", config::DEFAULT_PIPELINE_PREFIX)?;
    module.add("DEFAULT_TABLE_PREFIX", config::DEFAULT_TABLE_PREFIX)?;
    module.add("DEFAULT_VIEW_PREFIX", config::DEFAULT_VIEW_PREFIX)?;
    module.add(
        "RUN_UNRESPONSIVE_AFTER_SECONDS",
        config::RUN_UNRESPONSIVE_AFTER_SECONDS,
    )?;
    module.add(
        "DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS",
        config::DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS,
    )?;
    module.add(
        "DEFAULT_POSTGRES_PORT",
        source_registry::DEFAULT_POSTGRES_PORT,
    )?;
    module.add(
        "DEFAULT_KAFKA_FORMAT",
        source_registry::DEFAULT_KAFKA_FORMAT,
    )?;
    module.add(
        "DEFAULT_SQL_MODEL_ENGINE",
        model_spec::DEFAULT_SQL_MODEL_ENGINE,
    )?;
    module.add_function(wrap_pyfunction!(check_model_header_keys, module)?)?;
    module.add_function(wrap_pyfunction!(parse_model_spec, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_effective_configuration, module)?)?;
    module.add_function(wrap_pyfunction!(parse_source_registry, module)?)?;
    module.add_function(wrap_pyfunction!(parse_pipeline_mode, module)?)?;
    module.add_function(wrap_pyfunction!(parse_pipeline_settings, module)?)?;
    module.add_function(wrap_pyfunction!(parse_duration_seconds, module)?)?;
    module.add_function(wrap_pyfunction!(parse_freshness_policy, module)?)?;
    module.add_function(wrap_pyfunction!(parse_project_config, module)?)?;
    module.add_function(wrap_pyfunction!(parse_local_config, module)?)
}
