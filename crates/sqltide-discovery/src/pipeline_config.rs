//! The `pipeline.toml` schema. Its keys and mode are checked before the pipeline's models
//! load, and every other setting after, so the two halves are separate entry points.

use crate::config::{AuditDefaults, ReplayOnChange, ReplayRule};
use crate::durations::{duration_seconds, parse_duration_seconds};
use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::retention::{ModelRetention, Retention, is_identifier, parse_model_retention};
use crate::value::{Kind, Value};

const PIPELINE_CONFIG_KEYS: [&str; 8] = [
    "mode",
    "replay_on_change",
    "bounded_replay_fallback",
    "naming",
    "protection",
    "audit_defaults",
    "execution",
    "defaults",
];
const REPLAY_ON_CHANGE_KEYS: [&str; 2] = ["breaking", "non_breaking"];
const NAMING_KEYS: [&str; 2] = ["table_prefix", "view_prefix"];
const PROTECTION_KEYS: [&str; 2] = ["warning", "confirmation"];
const AUDIT_DEFAULT_KEYS: [&str; 3] = ["severity", "every", "warmup"];
const AUDIT_SEVERITIES: [&str; 2] = ["error", "warning"];
const EXECUTION_KEYS: [&str; 1] = ["replay"];
const REPLAY_EXECUTION_KEYS: [&str; 1] = ["settings"];
const DEFAULTS_KEYS: [&str; 1] = ["models"];
const MODEL_DEFAULT_KEYS: [&str; 1] = ["retention"];
const PIPELINE_MODES: [&str; 2] = ["direct", "virtual"];
const DIRECT_MODE: &str = "direct";
const FULL_REPLAY: &str = "full";
const BOUNDED_REPLAY_FALLBACKS: [&str; 2] = ["full", "bounded_without_history"];
const BOUNDED_REPLAY_PREFIX: &str = "bounded-";
const CONFIRMATION_PREFIX: &str = "CONFIRM_";

/// Everything in `pipeline.toml` besides its mode.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PipelineSettings {
    pub replay_on_change: Option<ReplayOnChange>,
    /// `full` or `bounded_without_history`.
    pub bounded_replay_fallback: Option<String>,
    pub table_prefix: Option<String>,
    pub view_prefix: Option<String>,
    /// `(warning, confirmation)`.
    pub protection: Option<(String, String)>,
    pub audit_defaults: AuditDefaults,
    /// Replay query settings in authored order, each value as Python `str()` renders it.
    pub replay_settings: Option<Vec<(String, String)>>,
    pub model_retention: Retention<ModelRetention>,
}

struct Parser<'a> {
    file_path: &'a str,
}

/// Check the top-level keys and resolve the mode, `default_mode` when unset.
///
/// # Errors
///
/// Returns a discovery error naming the file.
pub fn parse_pipeline_mode(
    payload: &Value,
    file_path: &str,
    default_mode: &str,
) -> SpecResult<String> {
    let parser: Parser<'_> = Parser { file_path };
    parser.allowed_keys(payload, &PIPELINE_CONFIG_KEYS, "")?;
    let Some(value) = field(payload, "mode") else {
        return Ok(default_mode.to_owned());
    };
    match value.as_str() {
        None => parser.error("must define mode as 'direct' or 'virtual'"),
        Some(mode) if PIPELINE_MODES.contains(&mode) => Ok(mode.to_owned()),
        Some(mode) => parser.error(format!(
            "has unsupported mode '{mode}'; expected 'direct' or 'virtual'"
        )),
    }
}

/// Validate every setting besides the mode, for pipeline `pipeline_name` in `mode`.
///
/// # Errors
///
/// Returns a discovery error naming the file.
pub fn parse_pipeline_settings(
    payload: &Value,
    file_path: &str,
    pipeline_name: &str,
    mode: &str,
) -> SpecResult<PipelineSettings> {
    let parser: Parser<'_> = Parser { file_path };
    let replay_on_change: Option<ReplayOnChange> =
        parser.replay_on_change(field(payload, "replay_on_change"))?;
    let bounded_replay_fallback: Option<String> = match field(payload, "bounded_replay_fallback") {
        None => None,
        Some(value) => match value.as_str() {
            Some(fallback) if BOUNDED_REPLAY_FALLBACKS.contains(&fallback) => {
                Some(fallback.to_owned())
            }
            _ => {
                return parser.error(format!(
                    "has unsupported bounded_replay_fallback '{}'; expected 'full' or 'bounded_without_history'",
                    value.python_str()
                ));
            }
        },
    };
    let (table_prefix, view_prefix) = parser.naming(field(payload, "naming"))?;
    let protection: Option<(String, String)> =
        parser.protection(field(payload, "protection"), pipeline_name, mode)?;
    let audit_defaults: AuditDefaults = parser.audit_defaults(field(payload, "audit_defaults"))?;
    let replay_settings: Option<Vec<(String, String)>> =
        parser.execution(field(payload, "execution"))?;
    let model_retention: Retention<ModelRetention> =
        parser.model_retention(field(payload, "defaults"))?;
    Ok(PipelineSettings {
        replay_on_change,
        bounded_replay_fallback,
        table_prefix,
        view_prefix,
        protection,
        audit_defaults,
        replay_settings,
        model_retention,
    })
}

fn field<'v>(mapping: &'v Value, key: &str) -> Option<&'v Value> {
    mapping.get(key).filter(|value| !value.is_none())
}

fn is_string_keyed_dict(value: &Value) -> bool {
    matches!(&value.kind, Kind::Dict(entries) if entries.iter().all(|(key, _)| key.as_str().is_some()))
}

impl Parser<'_> {
    fn error<T>(&self, message: impl std::fmt::Display) -> SpecResult<T> {
        Err(SpecError::new(
            ErrorKind::PipelineDiscovery,
            format!("Pipeline config '{}' {message}", self.file_path),
        ))
    }

    /// Reject keys outside `allowed`, sorted; `section` is `""` for the top level.
    fn allowed_keys(&self, mapping: &Value, allowed: &[&str], section: &str) -> SpecResult<()> {
        let mut unknown: Vec<String> = mapping
            .mapping_entries()
            .unwrap_or_default()
            .iter()
            .filter(|(key, _)| !key.as_str().is_some_and(|key| allowed.contains(&key)))
            .map(|(key, _)| key.python_str())
            .collect();
        if unknown.is_empty() {
            return Ok(());
        }
        unknown.sort_unstable();
        let separator: &str = if section.is_empty() { "" } else { " " };
        self.error(format!(
            "contains unsupported {section}{separator}keys: {}",
            unknown.join(", ")
        ))
    }

    fn replay_on_change(&self, value: Option<&Value>) -> SpecResult<Option<ReplayOnChange>> {
        let Some(value) = value else {
            return Ok(None);
        };
        if !is_string_keyed_dict(value) {
            return self.error("must define replay_on_change as a mapping");
        }
        self.allowed_keys(value, &REPLAY_ON_CHANGE_KEYS, "replay_on_change")?;
        Ok(Some(ReplayOnChange {
            breaking: self.replay_rule(field(value, "breaking"), "breaking")?,
            non_breaking: self.replay_rule(field(value, "non_breaking"), "non_breaking")?,
        }))
    }

    fn replay_rule(&self, value: Option<&Value>, key: &str) -> SpecResult<Option<ReplayRule>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let Some(text) = value.as_str() else {
            return self.error(format!("must define replay_on_change.{key} as a string"));
        };
        if text == FULL_REPLAY {
            return Ok(Some(ReplayRule::Full));
        }
        match text
            .strip_prefix(BOUNDED_REPLAY_PREFIX)
            .and_then(duration_seconds)
        {
            Some(lookback_seconds) => Ok(Some(ReplayRule::Bounded { lookback_seconds })),
            None => self.error(format!(
                "must define replay_on_change.{key} as 'full' or 'bounded-<duration>'"
            )),
        }
    }

    fn naming(&self, value: Option<&Value>) -> SpecResult<(Option<String>, Option<String>)> {
        let Some(value) = value else {
            return Ok((None, None));
        };
        if !is_string_keyed_dict(value) {
            return self.error("must define naming as a mapping");
        }
        self.allowed_keys(value, &NAMING_KEYS, "naming")?;
        let prefix = |key: &str| -> SpecResult<Option<String>> {
            match value.get(key) {
                None => Ok(None),
                Some(prefix) => match prefix.as_str() {
                    Some(text) => Ok(Some(text.to_owned())),
                    None => self.error(format!("must define naming.{key} as a string")),
                },
            }
        };
        Ok((prefix("table_prefix")?, prefix("view_prefix")?))
    }

    fn protection(
        &self,
        value: Option<&Value>,
        pipeline_name: &str,
        mode: &str,
    ) -> SpecResult<Option<(String, String)>> {
        let Some(value) = value else {
            return Ok(None);
        };
        if mode != DIRECT_MODE {
            return self.error("may define protection only for direct pipelines");
        }
        if !is_string_keyed_dict(value) {
            return self.error("must define protection as a mapping");
        }
        self.allowed_keys(value, &PROTECTION_KEYS, "protection")?;
        let text = |key: &str, default: String| -> SpecResult<String> {
            match value.get(key) {
                None => Ok(default),
                Some(text) => match text.as_str() {
                    Some(text) if !crate::text::python_strip(text).is_empty() => {
                        Ok(text.to_owned())
                    }
                    _ => self.error(format!(
                        "must define protection.{key} as a non-empty string"
                    )),
                },
            }
        };
        let warning: String = text(
            "warning",
            format!(
                "Pipeline '{pipeline_name}' is protected. Confirm its operational impact before building."
            ),
        )?;
        let confirmation: String = text("confirmation", default_confirmation(pipeline_name))?;
        if !is_confirmation(&confirmation) {
            return self.error(
                "protection.confirmation must contain only letters, numbers, '.', '_', ':', or '-' and must start with a letter or number",
            );
        }
        Ok(Some((warning, confirmation)))
    }

    fn audit_defaults(&self, value: Option<&Value>) -> SpecResult<AuditDefaults> {
        let Some(value) = value else {
            return Ok(AuditDefaults {
                severity: None,
                cadence_seconds: None,
                warmup_seconds: None,
            });
        };
        if !is_string_keyed_dict(value) {
            return self.error("must define audit_defaults as a mapping");
        }
        self.allowed_keys(value, &AUDIT_DEFAULT_KEYS, "audit_defaults")?;
        let severity: Option<String> = match field(value, "severity") {
            None => None,
            Some(severity) => match severity.as_str() {
                Some(text) if AUDIT_SEVERITIES.contains(&text) => Some(text.to_owned()),
                _ => return self.error("audit_defaults.severity must be 'error' or 'warning'"),
            },
        };
        let duration = |key: &str, allow_zero: bool| -> SpecResult<Option<u64>> {
            value
                .get(key)
                .map(|duration| {
                    parse_duration_seconds(
                        duration.as_str(),
                        &format!("Pipeline config '{}' audit_defaults.{key}", self.file_path),
                        allow_zero,
                    )
                    .map_err(|failure| failure.reraise(ErrorKind::PipelineDiscovery))
                })
                .transpose()
        };
        Ok(AuditDefaults {
            severity,
            cadence_seconds: duration("every", false)?,
            warmup_seconds: duration("warmup", true)?,
        })
    }

    fn execution(&self, value: Option<&Value>) -> SpecResult<Option<Vec<(String, String)>>> {
        let Some(value) = value else {
            return Ok(None);
        };
        if !matches!(value.kind, Kind::Dict(_)) {
            return self.error("must define execution as a mapping");
        }
        self.allowed_keys(value, &EXECUTION_KEYS, "execution")?;
        let Some(replay) = field(value, "replay") else {
            return Ok(None);
        };
        if !matches!(replay.kind, Kind::Dict(_)) {
            return self.error("must define execution.replay as a mapping");
        }
        self.allowed_keys(replay, &REPLAY_EXECUTION_KEYS, "execution.replay")?;
        let settings: &[(Value, Value)] = match field(replay, "settings")
            .map(|settings| &settings.kind)
        {
            Some(Kind::Dict(entries)) if !entries.is_empty() => entries,
            _ => {
                return self.error("must define execution.replay.settings as a non-empty mapping");
            }
        };
        let mut invalid: Vec<String> = settings
            .iter()
            .filter(|(name, _)| !name.as_str().is_some_and(is_identifier))
            .map(|(name, _)| name.python_str())
            .collect();
        if !invalid.is_empty() {
            invalid.sort_unstable();
            return self.error(format!(
                "contains invalid execution.replay.settings names: {}",
                invalid.join(", ")
            ));
        }
        Ok(Some(
            settings
                .iter()
                .map(|(name, setting)| (name.python_str(), setting.python_str()))
                .collect(),
        ))
    }

    fn model_retention(&self, value: Option<&Value>) -> SpecResult<Retention<ModelRetention>> {
        let Some(value) = value else {
            return Ok(Retention::Unset);
        };
        if !matches!(value.kind, Kind::Dict(_)) {
            return self.error("must define defaults as a mapping");
        }
        self.allowed_keys(value, &DEFAULTS_KEYS, "defaults")?;
        let models: &Value = match value.get("models") {
            Some(models) if matches!(models.kind, Kind::Dict(_)) => models,
            _ => return self.error("must define defaults.models as a mapping"),
        };
        self.allowed_keys(models, &MODEL_DEFAULT_KEYS, "defaults.models")?;
        parse_model_retention(
            models.get("retention"),
            &format!(
                "Pipeline config '{}' defaults.models.retention",
                self.file_path
            ),
        )
        .map_err(|failure| failure.reraise(ErrorKind::PipelineDiscovery))
    }
}

/// `[A-Za-z0-9][A-Za-z0-9._:-]*`.
fn is_confirmation(text: &str) -> bool {
    let mut characters = text.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        && characters.all(is_confirmation_character)
}

const fn is_confirmation_character(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | ':' | '-')
}

/// The pipeline name when it is a valid confirmation, else `CONFIRM_` and the name with each
/// disallowed character replaced by `_`.
fn default_confirmation(pipeline_name: &str) -> String {
    if is_confirmation(pipeline_name) {
        return pipeline_name.to_owned();
    }
    let sanitized: String = pipeline_name
        .chars()
        .map(|c| if is_confirmation_character(c) { c } else { '_' })
        .collect();
    format!("{CONFIRMATION_PREFIX}{sanitized}")
}
