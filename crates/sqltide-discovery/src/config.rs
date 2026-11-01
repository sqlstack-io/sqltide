//! The project and local configuration schema: strict validation of decoded TOML into typed
//! configuration, checked in a fixed order so the first problem reported is deterministic.
//!
//! Connection settings, variables, and interpolated retention templates pass through as
//! handles to the caller's original values.

use crate::INTERPOLATION_TOKEN_START;
use crate::byte_size::{ByteSize, parse_byte_size};
use crate::durations::{duration_seconds, parse_duration_seconds};
use crate::errors::{ErrorKind, SpecResult, project};
use crate::freshness::{Freshness, parse_freshness_policy};
use crate::retention::{
    KafkaRetention, ModelRetention, Retention, parse_kafka_retention, parse_model_retention,
};
use crate::text::python_strip;
use crate::value::{Kind, Value};

const PROJECT_CONFIG_KEYS: [&str; 15] = [
    "name",
    "adapter",
    "default_target",
    "connection",
    "vars",
    "targets",
    "defaults",
    "naming",
    "audit_scheduler",
    "sensors",
    "build",
    "ui",
    "dependencies",
    "rules",
    "settings",
];
const LOCAL_CONFIG_KEYS: [&str; 6] = [
    "target",
    "adapter",
    "defaults",
    "connection",
    "vars",
    "targets",
];
const TARGET_KEYS: [&str; 8] = [
    "database",
    "connection",
    "vars",
    "audit_scheduler",
    "sensors",
    "build",
    "destruction",
    "production",
];
const LOCAL_TARGET_KEYS: [&str; 5] = [
    "database",
    "connection",
    "vars",
    "audit_scheduler",
    "sensors",
];
const BUILD_KEYS: [&str; 1] = ["max_pipelines"];
const DESTRUCTION_KEYS: [&str; 1] = ["max_table_size_to_drop"];
const UI_KEYS: [&str; 1] = ["timezone"];
const DEPENDENCIES_KEYS: [&str; 2] = ["model_reference_scope", "allowed_cross_pipeline_references"];
const ALLOWED_CROSS_PIPELINE_REFERENCE_KEYS: [&str; 2] =
    ["upstream_pipeline", "downstream_pipeline"];
const AUDIT_SCHEDULER_KEYS: [&str; 1] = ["enabled"];
const SENSORS_KEYS: [&str; 3] = ["enabled", "tick_retention_days", "maximum_event_age"];
const DEFAULTS_KEYS: [&str; 12] = [
    "managed_source_ttl",
    "model_ttl",
    "kafka_broker_list",
    "pipeline_mode",
    "replay_on_change",
    "bounded_replay_fallback",
    "freshness",
    "audits",
    "deployment_readiness",
    "run_presumed_failed_after",
    "sources",
    "models",
];
const LOCAL_DEFAULTS_KEYS: [&str; 1] = ["pipeline_mode"];
const DEPLOYMENT_READINESS_KEYS: [&str; 2] = ["maximum_lag", "minimum_staged_row_ratio"];
const SOURCE_DEFAULT_KEYS: [&str; 1] = ["kafka"];
const KAFKA_SOURCE_DEFAULT_KEYS: [&str; 2] = ["naming_macro", "retention"];
const MODEL_DEFAULT_KEYS: [&str; 1] = ["retention"];
const PROJECT_NAMING_KEYS: [&str; 4] = [
    "pipeline_prefix",
    "pipeline_naming_macro",
    "table_prefix",
    "view_prefix",
];
const AUDIT_DEFAULT_KEYS: [&str; 3] = ["severity", "every", "warmup"];
const AUDIT_SEVERITIES: [&str; 2] = ["error", "warning"];
const REPLAY_ON_CHANGE_KEYS: [&str; 2] = ["breaking", "non_breaking"];
const PIPELINE_MODES: [&str; 2] = ["direct", "virtual"];
const MODEL_REFERENCE_SCOPES: [&str; 2] = ["project", "pipeline"];
const FULL_REPLAY: &str = "full";
const BOUNDED_WITHOUT_HISTORY: &str = "bounded_without_history";
const BOUNDED_REPLAY_PREFIX: &str = "bounded-";
pub const DEFAULT_ADAPTER_NAME: &str = "clickhouse";
pub const DEFAULT_PIPELINE_PREFIX: &str = "pl__";
pub const DEFAULT_TABLE_PREFIX: &str = "tbl__";
pub const DEFAULT_VIEW_PREFIX: &str = "view__";
const DEFAULT_TIMEZONE: &str = "UTC";
pub const DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS: u64 = 600;
const DEFAULT_MAXIMUM_LAG_SECONDS: u64 = 30;
const DEFAULT_MINIMUM_STAGED_ROW_RATIO: f64 = 0.5;
/// A run is unresponsive after this many seconds without a heartbeat.
pub const RUN_UNRESPONSIVE_AFTER_SECONDS: u64 = 45;

/// `(key, handle)` pairs sorted by key: connection settings or variables.
pub type Pairs = Vec<(String, usize)>;

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectConfig {
    pub name: String,
    pub adapter: String,
    pub default_target: String,
    pub connection: Pairs,
    pub variables: Pairs,
    pub targets: Vec<(String, ProjectTarget)>,
    pub defaults: ProjectDefaults,
    pub naming: ProjectNaming,
    pub audit_scheduler_enabled: bool,
    pub sensors: SensorsConfig,
    pub build: Build,
    pub ui_timezone: String,
    pub dependencies: Dependencies,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LocalConfig {
    pub target: Option<String>,
    pub adapter: Option<String>,
    /// `direct` or `virtual`.
    pub pipeline_mode: Option<String>,
    pub connection: Pairs,
    pub variables: Pairs,
    pub targets: Vec<(String, Target)>,
}

/// The settings a project target and a local target share.
#[derive(Clone, Debug, PartialEq)]
pub struct Target {
    pub database: Option<String>,
    pub connection: Pairs,
    pub variables: Pairs,
    pub audit_scheduler_enabled: Option<bool>,
    pub sensors: SensorsOverride,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectTarget {
    pub target: Target,
    pub production: bool,
    pub build: Build,
    pub max_table_size_to_drop_bytes: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Build {
    pub max_pipelines: Option<i128>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SensorsOverride {
    pub enabled: Option<bool>,
    pub tick_retention_days: Option<i128>,
    pub maximum_event_age_seconds: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SensorsConfig {
    pub enabled: bool,
    pub tick_retention_days: i128,
    pub maximum_event_age_seconds: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Dependencies {
    /// `project` or `pipeline`.
    pub model_reference_scope: String,
    /// `(upstream, downstream)` pipeline names.
    pub allowed_cross_pipeline_references: Vec<(String, String)>,
}

/// Prefixes left `None` keep their defaults.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectNaming {
    pub pipeline_prefix: Option<String>,
    pub pipeline_naming_macro: Option<String>,
    pub table_prefix: Option<String>,
    pub view_prefix: Option<String>,
}

/// A replay rule: full, or bounded to a lookback.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplayRule {
    Full,
    Bounded { lookback_seconds: u64 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReplayOnChange {
    pub breaking: Option<ReplayRule>,
    pub non_breaking: Option<ReplayRule>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuditDefaults {
    pub severity: Option<String>,
    pub cadence_seconds: Option<u64>,
    pub warmup_seconds: Option<u64>,
}

/// A retention policy, or a template containing `${` kept by handle for later resolution.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RetentionDefault<T> {
    Parsed(Retention<T>),
    Template(usize),
}

impl<T> RetentionDefault<T> {
    const fn is_set(&self) -> bool {
        !matches!(self, Self::Parsed(Retention::Unset))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectDefaults {
    pub managed_source_ttl: Option<String>,
    pub model_ttl: Option<String>,
    pub kafka_broker_list: Option<String>,
    /// `direct` or `virtual`.
    pub pipeline_mode: String,
    pub replay_on_change: Option<ReplayOnChange>,
    /// `full` or `bounded_without_history`.
    pub bounded_replay_fallback: Option<String>,
    pub run_presumed_failed_after_seconds: u64,
    pub freshness: Option<Freshness>,
    pub audits: AuditDefaults,
    pub maximum_lag_seconds: f64,
    pub minimum_staged_row_ratio: f64,
    pub kafka_naming_macro: Option<String>,
    pub kafka_retention: RetentionDefault<KafkaRetention>,
    pub model_retention: RetentionDefault<ModelRetention>,
}

/// Validates one configuration file; `timezone_exists` answers whether the runtime can load
/// an IANA timezone, since that depends on the host's timezone database.
struct Parser<'a> {
    file_path: &'a str,
    timezone_exists: &'a mut dyn FnMut(&str) -> bool,
}

/// Validate a committed project configuration document.
///
/// # Errors
///
/// Returns the first problem, as a project configuration error unless a duration or
/// freshness check owns it.
pub fn parse_project_config(
    payload: &Value,
    file_path: &str,
    timezone_exists: &mut dyn FnMut(&str) -> bool,
) -> SpecResult<ProjectConfig> {
    Parser {
        file_path,
        timezone_exists,
    }
    .project(payload)
}

/// Validate a local, uncommitted configuration document.
///
/// # Errors
///
/// Returns the first problem.
pub fn parse_local_config(payload: &Value, file_path: &str) -> SpecResult<LocalConfig> {
    Parser {
        file_path,
        timezone_exists: &mut |_| true,
    }
    .local(payload)
}

fn field<'v>(mapping: &'v Value, key: &str) -> Option<&'v Value> {
    mapping.get(key).filter(|value| !value.is_none())
}

const fn is_int(value: &Value) -> Option<i128> {
    match &value.kind {
        Kind::Int(Some(number), _) => Some(*number),
        _ => None,
    }
}

impl Parser<'_> {
    fn error<T>(&self, message: impl std::fmt::Display) -> SpecResult<T> {
        Err(project(format!("{} {message}", self.file_path)))
    }

    fn project(&mut self, payload: &Value) -> SpecResult<ProjectConfig> {
        self.allowed_keys(payload, &PROJECT_CONFIG_KEYS, "project")?;
        let settings = self.mapping(field(payload, "settings"), "settings")?;
        self.allowed_keys(settings, &["sql_analysis"], "settings")?;
        if let Some(value) = field(settings, "sql_analysis")
            && !matches!(value.kind, Kind::Bool(_))
        {
            return self.error("settings.sql_analysis must be a boolean");
        }
        let targets: Vec<(String, ProjectTarget)> = self
            .sorted_entries(field(payload, "targets"), "targets")?
            .into_iter()
            .map(|(name, value)| {
                Ok((
                    name.to_owned(),
                    self.project_target(value, &format!("targets.{name}"))?,
                ))
            })
            .collect::<SpecResult<_>>()?;
        let build: Build = self.build(field(payload, "build"), "build")?;
        if build.max_pipelines.is_none()
            && targets
                .iter()
                .any(|(_, target)| target.build.max_pipelines.is_some())
        {
            return self
                .error("target build limits require build.max_pipelines as a project default");
        }
        let name: String = self.require_non_empty(payload, "name", "project")?;
        if name.contains(INTERPOLATION_TOKEN_START) {
            return self
                .error("project.name must be a committed literal and cannot be interpolated");
        }
        let adapter: String = self
            .optional_non_empty(payload, "adapter", "project")?
            .unwrap_or_else(|| DEFAULT_ADAPTER_NAME.to_owned());
        let default_target: String =
            self.require_non_empty(payload, "default_target", "project")?;
        let connection: Pairs = self.pairs(field(payload, "connection"), "connection")?;
        let variables: Pairs = self.pairs(field(payload, "vars"), "project.vars")?;
        let defaults: ProjectDefaults = self.defaults(field(payload, "defaults"))?;
        let naming: ProjectNaming = self.naming(field(payload, "naming"))?;
        let audit_scheduler_enabled: bool = self
            .audit_scheduler(field(payload, "audit_scheduler"), "audit_scheduler")?
            .unwrap_or(false);
        let sensors: SensorsOverride = self.sensors(field(payload, "sensors"), "sensors")?;
        let ui_timezone: String = self.ui(field(payload, "ui"))?;
        let dependencies: Dependencies = self.dependencies(field(payload, "dependencies"))?;
        Ok(ProjectConfig {
            name,
            adapter,
            default_target,
            connection,
            variables,
            targets,
            defaults,
            naming,
            audit_scheduler_enabled,
            sensors: SensorsConfig {
                enabled: sensors.enabled.unwrap_or(false),
                tick_retention_days: sensors.tick_retention_days.unwrap_or(0),
                maximum_event_age_seconds: sensors.maximum_event_age_seconds,
            },
            build,
            ui_timezone,
            dependencies,
        })
    }

    fn local(&mut self, payload: &Value) -> SpecResult<LocalConfig> {
        self.allowed_keys(payload, &LOCAL_CONFIG_KEYS, "local project")?;
        let target: Option<String> = self.optional_non_empty(payload, "target", "local project")?;
        let adapter: Option<String> =
            self.optional_non_empty(payload, "adapter", "local project")?;
        let defaults: &Value = self.mapping(field(payload, "defaults"), "defaults")?;
        self.allowed_keys(defaults, &LOCAL_DEFAULTS_KEYS, "defaults")?;
        let pipeline_mode: Option<String> = match defaults.get("pipeline_mode") {
            Some(value) => Some(self.pipeline_mode(value, "defaults.pipeline_mode")?),
            None => None,
        };
        let connection: Pairs = self.pairs(field(payload, "connection"), "connection")?;
        let variables: Pairs = self.pairs(field(payload, "vars"), "local project.vars")?;
        let targets: Vec<(String, Target)> = self
            .sorted_entries(field(payload, "targets"), "targets")?
            .into_iter()
            .map(|(name, value)| {
                let label: String = format!("targets.{name}");
                let mapping: &Value = self.mapping(Some(value), &label)?;
                self.allowed_keys(mapping, &LOCAL_TARGET_KEYS, &label)?;
                Ok((name.to_owned(), self.target(mapping, &label)?))
            })
            .collect::<SpecResult<_>>()?;
        Ok(LocalConfig {
            target,
            adapter,
            pipeline_mode,
            connection,
            variables,
            targets,
        })
    }

    fn project_target(&mut self, value: &Value, label: &str) -> SpecResult<ProjectTarget> {
        let mapping: &Value = self.mapping(Some(value), label)?;
        self.allowed_keys(mapping, &TARGET_KEYS, label)?;
        let production: bool = match mapping.get("production") {
            None => false,
            Some(Value {
                kind: Kind::Bool(flag),
                ..
            }) => *flag,
            Some(_) => return self.error(format!("{label}.production must be a boolean")),
        };
        let target: Target = self.target(mapping, label)?;
        let build: Build = self.build(field(mapping, "build"), &format!("{label}.build"))?;
        let max_table_size_to_drop_bytes: Option<u64> = self.destruction(
            field(mapping, "destruction"),
            &format!("{label}.destruction"),
        )?;
        Ok(ProjectTarget {
            target,
            production,
            build,
            max_table_size_to_drop_bytes,
        })
    }

    fn target(&mut self, mapping: &Value, label: &str) -> SpecResult<Target> {
        Ok(Target {
            database: self.optional_non_empty(mapping, "database", label)?,
            connection: self.pairs(field(mapping, "connection"), "connection")?,
            variables: self.pairs(field(mapping, "vars"), &format!("{label}.vars"))?,
            audit_scheduler_enabled: self.audit_scheduler(
                field(mapping, "audit_scheduler"),
                &format!("{label}.audit_scheduler"),
            )?,
            sensors: self.sensors(field(mapping, "sensors"), &format!("{label}.sensors"))?,
        })
    }

    fn build(&self, payload: Option<&Value>, label: &str) -> SpecResult<Build> {
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &BUILD_KEYS, label)?;
        let max_pipelines: Option<i128> = match field(mapping, "max_pipelines") {
            None => None,
            Some(value) => match is_int(value) {
                Some(number) if number > 0 => Some(number),
                _ => {
                    return self.error(format!("{label}.max_pipelines must be a positive integer"));
                }
            },
        };
        Ok(Build { max_pipelines })
    }

    fn destruction(&self, payload: Option<&Value>, label: &str) -> SpecResult<Option<u64>> {
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &DESTRUCTION_KEYS, label)?;
        let Some(authored) = field(mapping, "max_table_size_to_drop") else {
            return Ok(None);
        };
        match authored.as_str().and_then(parse_byte_size) {
            Some(ByteSize::Bytes(limit)) if limit > 0 => {
                if let Ok(limit) = u64::try_from(limit) {
                    return Ok(Some(limit));
                }
            }
            _ => {}
        }
        self.error(format!(
            "{label}.max_table_size_to_drop must be a finite positive byte-size string such as '100GiB'"
        ))
    }

    fn ui(&mut self, payload: Option<&Value>) -> SpecResult<String> {
        let mapping: &Value = self.mapping(payload, "ui")?;
        self.allowed_keys(mapping, &UI_KEYS, "ui")?;
        let timezone: String = self
            .optional_non_empty(mapping, "timezone", "ui")?
            .unwrap_or_else(|| DEFAULT_TIMEZONE.to_owned());
        if !(self.timezone_exists)(&timezone) {
            return self.error("ui.timezone must be a valid IANA timezone such as 'Europe/London'");
        }
        Ok(timezone)
    }

    fn dependencies(&self, payload: Option<&Value>) -> SpecResult<Dependencies> {
        let mapping: &Value = self.mapping(payload, "dependencies")?;
        self.allowed_keys(mapping, &DEPENDENCIES_KEYS, "dependencies")?;
        let model_reference_scope: String = match mapping.get("model_reference_scope") {
            None => MODEL_REFERENCE_SCOPES[0].to_owned(),
            Some(value) => match value.as_str() {
                Some(scope) if MODEL_REFERENCE_SCOPES.contains(&scope) => scope.to_owned(),
                _ => {
                    return self.error(
                        "dependencies.model_reference_scope must be 'project' or 'pipeline'",
                    );
                }
            },
        };
        let mut references: Vec<(String, String)> = Vec::new();
        match field(mapping, "allowed_cross_pipeline_references") {
            None => {}
            Some(Value {
                kind: Kind::List(items),
                ..
            }) => {
                for (index, item) in items.iter().enumerate() {
                    let label: String = format!(
                        "dependencies.allowed_cross_pipeline_references[{}]",
                        index + 1
                    );
                    let item: &Value = self.mapping(Some(item), &label)?;
                    self.allowed_keys(item, &ALLOWED_CROSS_PIPELINE_REFERENCE_KEYS, &label)?;
                    let upstream: String =
                        self.require_non_empty(item, "upstream_pipeline", &label)?;
                    let downstream: String =
                        self.require_non_empty(item, "downstream_pipeline", &label)?;
                    if upstream == downstream {
                        return self.error(format!(
                            "{label} cannot allow a pipeline to reference itself"
                        ));
                    }
                    let pair: (String, String) = (upstream, downstream);
                    if references.contains(&pair) {
                        return self.error(format!("{label} duplicates {} -> {}", pair.0, pair.1));
                    }
                    references.push(pair);
                }
            }
            Some(_) => {
                return self.error(
                    "dependencies.allowed_cross_pipeline_references must be an array of tables",
                );
            }
        }
        Ok(Dependencies {
            model_reference_scope,
            allowed_cross_pipeline_references: references,
        })
    }

    fn sensors(&self, payload: Option<&Value>, label: &str) -> SpecResult<SensorsOverride> {
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &SENSORS_KEYS, label)?;
        let enabled: Option<bool> = self.optional_bool(mapping, label)?;
        let tick_retention_days: Option<i128> = match field(mapping, "tick_retention_days") {
            None => None,
            Some(value) => match is_int(value) {
                Some(days) if days >= 0 => Some(days),
                _ => {
                    return self.error(format!(
                        "{label}.tick_retention_days must be a non-negative integer"
                    ));
                }
            },
        };
        let maximum_event_age_seconds: Option<u64> = match field(mapping, "maximum_event_age") {
            None => None,
            Some(value) => Some(parse_duration_seconds(
                value.as_str(),
                &format!("{} {label}.maximum_event_age", self.file_path),
                false,
            )?),
        };
        Ok(SensorsOverride {
            enabled,
            tick_retention_days,
            maximum_event_age_seconds,
        })
    }

    fn audit_scheduler(&self, payload: Option<&Value>, label: &str) -> SpecResult<Option<bool>> {
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &AUDIT_SCHEDULER_KEYS, label)?;
        self.optional_bool(mapping, label)
    }

    fn optional_bool(&self, mapping: &Value, label: &str) -> SpecResult<Option<bool>> {
        match field(mapping, "enabled") {
            None => Ok(None),
            Some(Value {
                kind: Kind::Bool(flag),
                ..
            }) => Ok(Some(*flag)),
            Some(_) => self.error(format!("{label}.enabled must be a boolean")),
        }
    }

    fn defaults(&self, payload: Option<&Value>) -> SpecResult<ProjectDefaults> {
        let mapping: &Value = self.mapping(payload, "defaults")?;
        self.allowed_keys(mapping, &DEFAULTS_KEYS, "defaults")?;
        let run_presumed_failed_after_seconds: u64 = self
            .duration(mapping, "run_presumed_failed_after", "defaults", false)?
            .unwrap_or(DEFAULT_RUN_PRESUMED_FAILED_AFTER_SECONDS);
        if run_presumed_failed_after_seconds <= RUN_UNRESPONSIVE_AFTER_SECONDS {
            return self.error(format!(
                "defaults.run_presumed_failed_after must be longer than {RUN_UNRESPONSIVE_AFTER_SECONDS}s"
            ));
        }
        let managed_source_ttl: Option<String> =
            self.optional_non_empty(mapping, "managed_source_ttl", "defaults")?;
        let model_ttl: Option<String> =
            self.optional_non_empty(mapping, "model_ttl", "defaults")?;
        let sources: &Value = self.mapping(field(mapping, "sources"), "defaults.sources")?;
        self.allowed_keys(sources, &SOURCE_DEFAULT_KEYS, "defaults.sources")?;
        let kafka: &Value = self.mapping(field(sources, "kafka"), "defaults.sources.kafka")?;
        self.allowed_keys(kafka, &KAFKA_SOURCE_DEFAULT_KEYS, "defaults.sources.kafka")?;
        let kafka_naming_macro: Option<String> =
            self.optional_non_empty(kafka, "naming_macro", "defaults.sources.kafka")?;
        let kafka_retention: RetentionDefault<KafkaRetention> = self.retention(
            kafka.get("retention"),
            "defaults.sources.kafka.retention",
            parse_kafka_retention,
        )?;
        let models: &Value = self.mapping(field(mapping, "models"), "defaults.models")?;
        self.allowed_keys(models, &MODEL_DEFAULT_KEYS, "defaults.models")?;
        let model_retention: RetentionDefault<ModelRetention> = self.retention(
            models.get("retention"),
            "defaults.models.retention",
            parse_model_retention,
        )?;
        if managed_source_ttl.is_some() && kafka_retention.is_set() {
            return self.error(
                "defaults.managed_source_ttl cannot be combined with defaults.sources.kafka.retention",
            );
        }
        if model_ttl.is_some() && model_retention.is_set() {
            return self
                .error("defaults.model_ttl cannot be combined with defaults.models.retention");
        }
        let kafka_broker_list: Option<String> =
            self.optional_non_empty(mapping, "kafka_broker_list", "defaults")?;
        let pipeline_mode: String = match mapping.get("pipeline_mode") {
            None => PIPELINE_MODES[0].to_owned(),
            Some(value) => self.pipeline_mode(value, "defaults.pipeline_mode")?,
        };
        let replay_on_change: Option<ReplayOnChange> =
            self.replay_on_change(field(mapping, "replay_on_change"))?;
        let bounded_replay_fallback: Option<String> = match field(
            mapping,
            "bounded_replay_fallback",
        ) {
            None => None,
            Some(value) => match value.as_str() {
                Some(fallback) if [FULL_REPLAY, BOUNDED_WITHOUT_HISTORY].contains(&fallback) => {
                    Some(fallback.to_owned())
                }
                _ => {
                    return self.error(
                        "defaults.bounded_replay_fallback must be 'full' or 'bounded_without_history'",
                    );
                }
            },
        };
        let freshness: Option<Freshness> = parse_freshness_policy(
            mapping.get("freshness"),
            "defaults.freshness",
            self.file_path,
        )?;
        let audits: AuditDefaults = self.audit_defaults(field(mapping, "audits"))?;
        let readiness_label: &str = "defaults.deployment_readiness";
        let readiness: &Value =
            self.mapping(field(mapping, "deployment_readiness"), readiness_label)?;
        self.allowed_keys(readiness, &DEPLOYMENT_READINESS_KEYS, readiness_label)?;
        let maximum_lag: u64 = self
            .duration(readiness, "maximum_lag", readiness_label, true)?
            .unwrap_or(DEFAULT_MAXIMUM_LAG_SECONDS);
        #[allow(clippy::cast_precision_loss)]
        let minimum_staged_row_ratio: f64 = match readiness.get("minimum_staged_row_ratio") {
            None => DEFAULT_MINIMUM_STAGED_ROW_RATIO,
            Some(Value {
                kind: Kind::Int(Some(number), _),
                ..
            }) => *number as f64,
            Some(Value {
                kind: Kind::Float(number, _),
                ..
            }) => *number,
            Some(_) => f64::NAN,
        };
        if !(0.0..=1.0).contains(&minimum_staged_row_ratio) {
            return self.error(format!(
                "{readiness_label}.minimum_staged_row_ratio must be a number from 0 to 1"
            ));
        }
        #[allow(clippy::cast_precision_loss)]
        let maximum_lag_seconds: f64 = maximum_lag as f64;
        Ok(ProjectDefaults {
            managed_source_ttl,
            model_ttl,
            kafka_broker_list,
            pipeline_mode,
            replay_on_change,
            bounded_replay_fallback,
            run_presumed_failed_after_seconds,
            freshness,
            audits,
            maximum_lag_seconds,
            minimum_staged_row_ratio,
            kafka_naming_macro,
            kafka_retention,
            model_retention,
        })
    }

    /// A duration under `key`, `None` when absent; failures are configuration errors.
    fn duration(
        &self,
        mapping: &Value,
        key: &str,
        label: &str,
        allow_zero: bool,
    ) -> SpecResult<Option<u64>> {
        let Some(value) = mapping.get(key) else {
            return Ok(None);
        };
        parse_duration_seconds(
            value.as_str(),
            &format!("{} {label}.{key}", self.file_path),
            allow_zero,
        )
        .map(Some)
        .map_err(|failure| failure.reraise(ErrorKind::ProjectConfig))
    }

    fn retention<T>(
        &self,
        value: Option<&Value>,
        field_path: &str,
        parse: fn(Option<&Value>, &str) -> SpecResult<Retention<T>>,
    ) -> SpecResult<RetentionDefault<T>> {
        if let Some(template) = value.filter(|value| value.contains_interpolation()) {
            return Ok(RetentionDefault::Template(template.handle));
        }
        parse(value, &format!("{} {field_path}", self.file_path))
            .map(RetentionDefault::Parsed)
            .map_err(|failure| failure.reraise(ErrorKind::ProjectConfig))
    }

    fn audit_defaults(&self, payload: Option<&Value>) -> SpecResult<AuditDefaults> {
        let label: &str = "defaults.audits";
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &AUDIT_DEFAULT_KEYS, label)?;
        let severity: Option<String> = match field(mapping, "severity") {
            None => None,
            Some(value) => match value.as_str() {
                Some(severity) if AUDIT_SEVERITIES.contains(&severity) => Some(severity.to_owned()),
                _ => return self.error(format!("{label}.severity must be 'error' or 'warning'")),
            },
        };
        Ok(AuditDefaults {
            severity,
            cadence_seconds: self.duration(mapping, "every", label, false)?,
            warmup_seconds: self.duration(mapping, "warmup", label, true)?,
        })
    }

    fn pipeline_mode(&self, value: &Value, label: &str) -> SpecResult<String> {
        match value.as_str() {
            Some(mode) if PIPELINE_MODES.contains(&mode) => Ok(mode.to_owned()),
            _ => self.error(format!("{label} must be 'direct' or 'virtual'")),
        }
    }

    fn naming(&self, payload: Option<&Value>) -> SpecResult<ProjectNaming> {
        let mapping: &Value = self.mapping(payload, "naming")?;
        self.allowed_keys(mapping, &PROJECT_NAMING_KEYS, "naming")?;
        let prefix = |key: &str| -> SpecResult<Option<String>> {
            match mapping.get(key) {
                None => Ok(None),
                Some(value) => match value.as_str() {
                    Some(text) => Ok(Some(text.to_owned())),
                    None => self.error(format!("naming.{key} must be a string")),
                },
            }
        };
        Ok(ProjectNaming {
            pipeline_prefix: prefix("pipeline_prefix")?,
            pipeline_naming_macro: self.optional_non_empty(
                mapping,
                "pipeline_naming_macro",
                "naming",
            )?,
            table_prefix: prefix("table_prefix")?,
            view_prefix: prefix("view_prefix")?,
        })
    }

    fn replay_on_change(&self, payload: Option<&Value>) -> SpecResult<Option<ReplayOnChange>> {
        let label: &str = "defaults.replay_on_change";
        if payload.is_none() {
            return Ok(None);
        }
        let mapping: &Value = self.mapping(payload, label)?;
        self.allowed_keys(mapping, &REPLAY_ON_CHANGE_KEYS, label)?;
        Ok(Some(ReplayOnChange {
            breaking: self.replay_rule(field(mapping, "breaking"), &format!("{label}.breaking"))?,
            non_breaking: self.replay_rule(
                field(mapping, "non_breaking"),
                &format!("{label}.non_breaking"),
            )?,
        }))
    }

    fn replay_rule(&self, value: Option<&Value>, label: &str) -> SpecResult<Option<ReplayRule>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let Some(text) = value.as_str() else {
            return self.error(format!("{label} must be a string"));
        };
        if text == FULL_REPLAY {
            return Ok(Some(ReplayRule::Full));
        }
        match text
            .strip_prefix(BOUNDED_REPLAY_PREFIX)
            .and_then(duration_seconds)
        {
            Some(lookback_seconds) => Ok(Some(ReplayRule::Bounded { lookback_seconds })),
            None => self.error(format!("{label} must be 'full' or 'bounded-<duration>'")),
        }
    }

    /// An optional `dict` with string keys and no interpolated keys; absent means empty.
    fn mapping<'v>(&self, payload: Option<&'v Value>, label: &str) -> SpecResult<&'v Value> {
        static EMPTY: Value = Value {
            handle: usize::MAX,
            kind: Kind::Dict(Vec::new()),
            text: None,
        };
        let Some(payload) = payload.filter(|payload| !payload.is_none()) else {
            return Ok(&EMPTY);
        };
        let Kind::Dict(entries) = &payload.kind else {
            return self.error(format!("{label} must be a mapping"));
        };
        let mut interpolated: Vec<&str> = Vec::new();
        for (key, _) in entries {
            let Some(key) = key.as_str() else {
                return self.error(format!("{label} must be a mapping"));
            };
            if key.contains(INTERPOLATION_TOKEN_START) {
                interpolated.push(key);
            }
        }
        if !interpolated.is_empty() {
            interpolated.sort_unstable();
            return self.error(format!(
                "{label} must not interpolate mapping keys: {}",
                interpolated.join(", ")
            ));
        }
        Ok(payload)
    }

    /// The entries of an optional mapping, sorted by key.
    fn sorted_entries<'v>(
        &self,
        payload: Option<&'v Value>,
        label: &str,
    ) -> SpecResult<Vec<(&'v str, &'v Value)>> {
        let mapping: &'v Value = self.mapping(payload, label)?;
        let mut entries: Vec<(&str, &Value)> = mapping
            .mapping_entries()
            .unwrap_or_default()
            .iter()
            .filter_map(|(key, value)| Some((key.as_str()?, value)))
            .collect();
        entries.sort_by(|left, right| left.0.cmp(right.0));
        Ok(entries)
    }

    fn pairs(&self, payload: Option<&Value>, label: &str) -> SpecResult<Pairs> {
        Ok(self
            .sorted_entries(payload, label)?
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value.handle))
            .collect())
    }

    fn allowed_keys(&self, mapping: &Value, allowed: &[&str], label: &str) -> SpecResult<()> {
        let mut unknown: Vec<&str> = mapping
            .mapping_entries()
            .unwrap_or_default()
            .iter()
            .filter_map(|(key, _)| key.as_str())
            .filter(|key| !allowed.contains(key))
            .collect();
        if unknown.is_empty() {
            return Ok(());
        }
        unknown.sort_unstable();
        self.error(format!(
            "{label} contains unsupported keys: {}",
            unknown.join(", ")
        ))
    }

    fn optional_non_empty(
        &self,
        mapping: &Value,
        key: &str,
        label: &str,
    ) -> SpecResult<Option<String>> {
        let Some(value) = field(mapping, key) else {
            return Ok(None);
        };
        match value.as_str().map(python_strip) {
            Some(text) if !text.is_empty() => Ok(Some(text.to_owned())),
            _ => self.error(format!("{label}.{key} must be a non-empty string")),
        }
    }

    fn require_non_empty(&self, mapping: &Value, key: &str, label: &str) -> SpecResult<String> {
        match self.optional_non_empty(mapping, key, label)? {
            Some(text) => Ok(text),
            None => self.error(format!("{label} must define non-empty string '{key}'")),
        }
    }
}
