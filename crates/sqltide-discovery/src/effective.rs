//! Effective project configuration: the selected target, merged by precedence (command
//! line, local, local target, project target, project), with variables resolved and string
//! settings and retention templates interpolated. Connection settings stay uninterpolated.

use std::collections::{BTreeMap, HashMap};

use crate::config::{
    Build, DEFAULT_PIPELINE_PREFIX, DEFAULT_TABLE_PREFIX, DEFAULT_VIEW_PREFIX, Dependencies,
    LocalConfig, Pairs, ProjectConfig, ProjectDefaults, ProjectTarget, RetentionDefault,
    SensorsConfig, Target,
};
use crate::errors::{ErrorKind, SpecError, SpecResult, project};
use crate::interpolation::{self, Environment};
use crate::retention::{Retention, parse_kafka_retention, parse_model_retention};
use crate::value::{Value, from_interpolation};

/// Resolved naming prefixes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Naming {
    pub pipeline_prefix: String,
    pub pipeline_naming_macro: Option<String>,
    pub table_prefix: String,
    pub view_prefix: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct EffectiveConfig {
    pub name: String,
    pub adapter: String,
    pub target_name: String,
    pub production_target: bool,
    pub database: Option<String>,
    /// Merged connection settings, sorted by key.
    pub connection: Pairs,
    /// Resolved variables, sorted by name.
    pub variables: Vec<(String, interpolation::Value)>,
    /// Retention defaults are always `Parsed` here.
    pub defaults: ProjectDefaults,
    pub naming: Naming,
    pub audit_scheduler_enabled: bool,
    pub sensors: SensorsConfig,
    pub build: Build,
    pub max_table_size_to_drop_bytes: Option<u64>,
    pub ui_timezone: String,
    pub dependencies: Dependencies,
}

/// Inputs from outside the configuration files.
pub struct Invocation<'a> {
    pub selected_target: Option<&'a str>,
    /// Command-line variables, in the order given.
    pub cli_variables: &'a [(String, &'a Value)],
    pub environment: &'a dyn Environment,
    /// The committed project file, which messages name.
    pub project_path: &'a str,
}

/// Resolve the effective configuration; `objects` maps every handle the configurations and
/// command-line variables refer to.
///
/// # Errors
///
/// Returns a project configuration error for an unknown target, an unresolvable variable, or
/// a setting that does not resolve to a string or valid retention policy.
pub fn resolve_effective_config(
    project_config: &ProjectConfig,
    local: &LocalConfig,
    objects: &HashMap<usize, &Value>,
    invocation: &Invocation<'_>,
) -> SpecResult<EffectiveConfig> {
    let target_name: String = invocation
        .selected_target
        .filter(|name| !name.is_empty())
        .or(local.target.as_deref())
        .unwrap_or(&project_config.default_target)
        .to_owned();
    let project_target: Option<&ProjectTarget> = project_config
        .targets
        .iter()
        .find_map(|(name, target)| (*name == target_name).then_some(target));
    let local_target: Option<&Target> = local
        .targets
        .iter()
        .find_map(|(name, target)| (*name == target_name).then_some(target));
    if project_target.is_none() && local_target.is_none() {
        return Err(project(format!("Unknown target '{target_name}'")));
    }
    let project_side: Option<&Target> = project_target.map(|target| &target.target);
    let empty: Pairs = Vec::new();
    let pairs_of = |target: Option<&Target>, pick: fn(&Target) -> &Pairs| -> Vec<(String, usize)> {
        target.map_or(&empty, pick).clone()
    };
    let authored: Vec<(String, usize)> = project_config
        .variables
        .iter()
        .chain(&pairs_of(project_side, |target| &target.variables))
        .chain(&pairs_of(local_target, |target| &target.variables))
        .chain(&local.variables)
        .cloned()
        .collect();
    let mut merged: Vec<(String, &Value)> = Vec::new();
    let layers = authored
        .iter()
        .map(|(key, handle)| (key, objects[handle]))
        .chain(
            invocation
                .cli_variables
                .iter()
                .map(|(key, value)| (key, *value)),
        );
    for (key, value) in layers {
        match merged.iter_mut().find(|(name, _)| name == key) {
            Some(entry) => entry.1 = value,
            None => merged.push((key.clone(), value)),
        }
    }
    let values: BTreeMap<String, interpolation::Value> = merged
        .into_iter()
        .map(|(key, value)| Ok((key, value.to_interpolation()?)))
        .collect::<SpecResult<_>>()?;
    let resolved: Vec<(String, interpolation::Value)> = interpolation::resolve_variable_values(
        &values,
        invocation.environment,
        invocation.project_path,
        true,
    )
    .map_err(project)?;
    let resolver: Resolver<'_> = Resolver {
        variables: resolved.iter().cloned().collect(),
        environment: invocation.environment,
        objects,
        project_path: invocation.project_path,
    };
    let database: Option<String> = resolver.optional_string(
        local_target
            .and_then(|target| target.database.as_deref())
            .or_else(|| project_side.and_then(|target| target.database.as_deref())),
        &format!("targets.{target_name}.database"),
    )?;
    let adapter: String = resolver.string(
        local.adapter.as_deref().unwrap_or(&project_config.adapter),
        "adapter",
    )?;
    let mut connection: Vec<(String, usize)> = Vec::new();
    for (key, handle) in project_config
        .connection
        .iter()
        .chain(&pairs_of(project_side, |target| &target.connection))
        .chain(&pairs_of(local_target, |target| &target.connection))
        .chain(&local.connection)
    {
        match connection.iter_mut().find(|(name, _)| name == key) {
            Some(entry) => entry.1 = *handle,
            None => connection.push((key.clone(), *handle)),
        }
    }
    connection.sort_by(|left, right| left.0.cmp(&right.0));
    let mut variables: Vec<(String, interpolation::Value)> = resolved;
    variables.sort_by(|left, right| left.0.cmp(&right.0));
    let defaults: ProjectDefaults = resolver.defaults(&project_config.defaults, local)?;
    let naming: Naming = resolver.naming(project_config)?;
    let local_sensors = local_target.map(|target| target.sensors);
    let project_sensors = project_side.map(|target| target.sensors);
    Ok(EffectiveConfig {
        name: project_config.name.clone(),
        adapter,
        target_name,
        production_target: project_target.is_some_and(|target| target.production),
        database,
        connection,
        variables,
        defaults,
        naming,
        audit_scheduler_enabled: local_target
            .and_then(|target| target.audit_scheduler_enabled)
            .or_else(|| project_side.and_then(|target| target.audit_scheduler_enabled))
            .unwrap_or(project_config.audit_scheduler_enabled),
        sensors: SensorsConfig {
            enabled: local_sensors
                .and_then(|sensors| sensors.enabled)
                .or_else(|| project_sensors.and_then(|sensors| sensors.enabled))
                .unwrap_or(project_config.sensors.enabled),
            tick_retention_days: local_sensors
                .and_then(|sensors| sensors.tick_retention_days)
                .or_else(|| project_sensors.and_then(|sensors| sensors.tick_retention_days))
                .unwrap_or(project_config.sensors.tick_retention_days),
            maximum_event_age_seconds: local_sensors
                .and_then(|sensors| sensors.maximum_event_age_seconds)
                .or_else(|| project_sensors.and_then(|sensors| sensors.maximum_event_age_seconds))
                .or(project_config.sensors.maximum_event_age_seconds),
        },
        build: Build {
            max_pipelines: project_target
                .and_then(|target| target.build.max_pipelines)
                .or(project_config.build.max_pipelines),
        },
        max_table_size_to_drop_bytes: project_target
            .and_then(|target| target.max_table_size_to_drop_bytes),
        ui_timezone: project_config.ui_timezone.clone(),
        dependencies: project_config.dependencies.clone(),
    })
}

struct Resolver<'a> {
    variables: BTreeMap<String, interpolation::Value>,
    environment: &'a dyn Environment,
    objects: &'a HashMap<usize, &'a Value>,
    project_path: &'a str,
}

impl Resolver<'_> {
    fn interpolate(
        &self,
        value: &interpolation::Value,
        field_path: &str,
    ) -> SpecResult<interpolation::Value> {
        interpolation::interpolate_config_value(
            value,
            &self.variables,
            self.environment,
            field_path,
        )
        .map_err(project)
    }

    /// An interpolated setting, which must resolve to a string.
    fn string(&self, text: &str, setting: &str) -> SpecResult<String> {
        let field_path: String = format!("{} {setting}", self.project_path);
        match self.interpolate(&interpolation::Value::Str(text.to_owned()), &field_path)? {
            interpolation::Value::Str(text) => Ok(text),
            _ => Err(project(format!("{field_path} must resolve to a string"))),
        }
    }

    fn optional_string(&self, text: Option<&str>, setting: &str) -> SpecResult<Option<String>> {
        text.map(|text| self.string(text, setting)).transpose()
    }

    fn retention<T>(
        &self,
        retention: &RetentionDefault<T>,
        setting: &str,
        parse: fn(Option<&Value>, &str) -> SpecResult<Retention<T>>,
    ) -> SpecResult<RetentionDefault<T>>
    where
        T: Clone,
    {
        let RetentionDefault::Template(handle) = retention else {
            return Ok(retention.clone());
        };
        let field_path: String = format!("{} {setting}", self.project_path);
        let template: interpolation::Value = self.objects[handle].to_interpolation()?;
        let interpolated: Value =
            from_interpolation(self.objects, self.interpolate(&template, &field_path)?);
        parse(Some(&interpolated), &field_path)
            .map(RetentionDefault::Parsed)
            .map_err(|failure: SpecError| match failure.kind {
                ErrorKind::Type => failure,
                _ => failure.reraise(ErrorKind::ProjectConfig),
            })
    }

    fn defaults(
        &self,
        defaults: &ProjectDefaults,
        local: &LocalConfig,
    ) -> SpecResult<ProjectDefaults> {
        Ok(ProjectDefaults {
            pipeline_mode: local
                .pipeline_mode
                .clone()
                .unwrap_or_else(|| defaults.pipeline_mode.clone()),
            managed_source_ttl: self.optional_string(
                defaults.managed_source_ttl.as_deref(),
                "defaults.managed_source_ttl",
            )?,
            model_ttl: self.optional_string(defaults.model_ttl.as_deref(), "defaults.model_ttl")?,
            kafka_broker_list: self.optional_string(
                defaults.kafka_broker_list.as_deref(),
                "defaults.kafka_broker_list",
            )?,
            kafka_naming_macro: self.optional_string(
                defaults.kafka_naming_macro.as_deref(),
                "defaults.sources.kafka.naming_macro",
            )?,
            kafka_retention: self.retention(
                &defaults.kafka_retention,
                "defaults.sources.kafka.retention",
                parse_kafka_retention,
            )?,
            model_retention: self.retention(
                &defaults.model_retention,
                "defaults.models.retention",
                parse_model_retention,
            )?,
            ..defaults.clone()
        })
    }

    fn naming(&self, project_config: &ProjectConfig) -> SpecResult<Naming> {
        let naming = &project_config.naming;
        Ok(Naming {
            pipeline_prefix: self.string(
                naming
                    .pipeline_prefix
                    .as_deref()
                    .unwrap_or(DEFAULT_PIPELINE_PREFIX),
                "naming.pipeline_prefix",
            )?,
            pipeline_naming_macro: self.optional_string(
                naming.pipeline_naming_macro.as_deref(),
                "naming.pipeline_naming_macro",
            )?,
            table_prefix: self.string(
                naming
                    .table_prefix
                    .as_deref()
                    .unwrap_or(DEFAULT_TABLE_PREFIX),
                "naming.table_prefix",
            )?,
            view_prefix: self.string(
                naming.view_prefix.as_deref().unwrap_or(DEFAULT_VIEW_PREFIX),
                "naming.view_prefix",
            )?,
        })
    }
}
