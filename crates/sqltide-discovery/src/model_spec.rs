//! The `MODEL(...)` header schema of a SQL model: table and view fields, columns, audits,
//! retention, settings, and replay policy.
//!
//! Views check their query's references, and tables infer their driving input, at a fixed
//! point among the field checks; the caller supplies that query check.

use crate::config::{ReplayOnChange, ReplayRule};
use crate::durations::duration_seconds;
use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::retention::{ModelRetention, Retention, is_identifier, parse_model_retention};
use crate::text::python_strip;
use crate::value::{Kind, Value};

const ALLOWED_MODEL_KEYS: [&str; 16] = [
    "kind",
    "description",
    "columns",
    "audits",
    "relation_name",
    "engine",
    "order_by",
    "partition_by",
    "ttl",
    "retention",
    "settings",
    "execution_settings",
    "replay_anchor",
    "replay_on_change",
    "bounded_replay_fallback",
    "sql_analysis",
];
const VIEW_FORBIDDEN_KEYS: [&str; 10] = [
    "engine",
    "order_by",
    "partition_by",
    "ttl",
    "retention",
    "settings",
    "execution_settings",
    "replay_anchor",
    "replay_on_change",
    "bounded_replay_fallback",
];
const COLUMN_KEYS: [&str; 2] = ["description", "audits"];
const EXECUTION_SETTINGS_KEYS: [&str; 1] = ["replay"];
const REPLAY_ON_CHANGE_KEYS: [&str; 2] = ["breaking", "non_breaking"];
const REPLAY_ANCHORS: [&str; 2] = ["auto", "never"];
const BOUNDED_REPLAY_FALLBACKS: [&str; 2] = ["full", "bounded_without_history"];
const TABLE_KIND: &str = "table";
const VIEW_KIND: &str = "view";
const FULL_REPLAY: &str = "full";
const BOUNDED_REPLAY_PREFIX: &str = "bounded-";
pub const DEFAULT_SQL_MODEL_ENGINE: &str = "MergeTree()";
pub const DEFAULT_SQL_MODEL_ORDER_BY: &str = "_replay_timestamp";

/// One declared column; `audits` are the authored audit values, by handle.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ColumnSpec {
    pub name: String,
    pub description: Option<String>,
    pub audits: Vec<usize>,
}

/// Fields tables and views share.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommonSpec {
    pub relation_name: Option<String>,
    pub description: Option<String>,
    pub columns: Vec<ColumnSpec>,
    pub audits: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TableSpec {
    pub source: String,
    pub engine: String,
    pub order_by: Vec<String>,
    pub partition_by: Option<String>,
    pub ttl: Option<String>,
    pub retention: Retention<ModelRetention>,
    /// Table settings in authored order, each value as Python `str()` renders it.
    pub settings: Option<Vec<(String, String)>>,
    pub replay_settings: Option<Vec<(String, String)>>,
    /// `auto` or `never`.
    pub replay_anchor: String,
    pub replay_on_change: Option<ReplayOnChange>,
    /// `full` or `bounded_without_history`.
    pub bounded_replay_fallback: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ModelSpec {
    View(CommonSpec),
    Table(CommonSpec, Box<TableSpec>),
}

/// Check a query: `view` validates a view's references; `table` returns the driving input.
pub type QueryCheck<'a> = dyn FnMut(&str) -> SpecResult<Option<String>> + 'a;

struct Parser<'a> {
    file_path: &'a str,
}

/// Reject header keys outside the model schema.
///
/// # Errors
///
/// Returns a discovery error listing the sorted unknown keys.
pub fn check_model_header_keys(header: &Value, file_path: &str) -> SpecResult<()> {
    if let Some(value) = header.get("sql_analysis")
        && !matches!(value.kind, Kind::Bool(_))
    {
        return Err(discovery(format!(
            "MODEL(...) in '{file_path}' sql_analysis must be a boolean"
        )));
    }
    let mut unknown: Vec<String> = header
        .mapping_entries()
        .unwrap_or_default()
        .iter()
        .filter(|(key, _)| {
            !key.as_str()
                .is_some_and(|key| ALLOWED_MODEL_KEYS.contains(&key))
        })
        .map(|(key, _)| key.python_str())
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort_unstable();
    Err(discovery(format!(
        "MODEL(...) in '{file_path}' contains unsupported keys: {}",
        unknown.join(", ")
    )))
}

/// Validate a parsed header into a table or view spec.
///
/// # Errors
///
/// Returns the first problem, including one raised by `check_query`.
pub fn model_spec(
    header: &Value,
    file_path: &str,
    check_query: &mut QueryCheck<'_>,
) -> SpecResult<ModelSpec> {
    let parser: Parser<'_> = Parser { file_path };
    let kind: &str = match header.get("kind") {
        None => TABLE_KIND,
        Some(value) => match value.as_str() {
            Some(kind) if kind == TABLE_KIND || kind == VIEW_KIND => kind,
            _ => return parser.error("must define 'kind' as 'table' or 'view'"),
        },
    };
    let common: CommonSpec = CommonSpec {
        relation_name: parser.optional_string(header, "relation_name")?,
        description: parser.optional_string(header, "description")?,
        columns: parser.columns(field(header, "columns"))?,
        audits: parser.audits(field(header, "audits"), "audits")?,
    };
    if kind == VIEW_KIND {
        let mut forbidden: Vec<&str> = header
            .mapping_entries()
            .unwrap_or_default()
            .iter()
            .filter_map(|(key, _)| key.as_str())
            .filter(|key| VIEW_FORBIDDEN_KEYS.contains(key))
            .collect();
        if !forbidden.is_empty() {
            forbidden.sort_unstable();
            return Err(discovery(format!(
                "View MODEL(...) in '{file_path}' cannot define table/replay fields: {}",
                forbidden.join(", ")
            )));
        }
        check_query(VIEW_KIND)?;
        return Ok(ModelSpec::View(common));
    }
    let ttl: Option<String> = parser.optional_string(header, "ttl")?;
    let retention: Retention<ModelRetention> = parse_model_retention(
        header.get("retention"),
        &format!("SQL model '{file_path}' retention"),
    )
    .map_err(|failure| failure.reraise(ErrorKind::PipelineDiscovery))?;
    if ttl.is_some() && !matches!(retention, Retention::Unset) {
        return Err(discovery(format!(
            "SQL model '{file_path}' cannot combine ttl with typed retention"
        )));
    }
    let source: String = check_query(TABLE_KIND)?.unwrap_or_default();
    let engine: String = match field(header, "engine") {
        None => DEFAULT_SQL_MODEL_ENGINE.to_owned(),
        Some(value) => match value.as_str() {
            Some(engine) if !engine.is_empty() => engine.to_owned(),
            _ => return parser.error("must define 'engine' as a non-empty string"),
        },
    };
    let order_by: Vec<String> = match field(header, "order_by") {
        None => vec![DEFAULT_SQL_MODEL_ORDER_BY.to_owned()],
        Some(Value {
            kind: Kind::List(items),
            ..
        }) if !items.is_empty() && items.iter().all(|item| item.as_str().is_some()) => items
            .iter()
            .filter_map(|item| item.as_str().map(str::to_owned))
            .collect(),
        Some(_) => return parser.error("must define 'order_by' as a non-empty list of strings"),
    };
    let partition_by: Option<String> = parser.optional_string(header, "partition_by")?;
    let settings: Option<Vec<(String, String)>> = match field(header, "settings") {
        None => None,
        Some(value) => match &value.kind {
            Kind::Dict(entries) if entries.iter().all(|(key, _)| key.as_str().is_some()) => Some(
                entries
                    .iter()
                    .map(|(key, item)| (key.python_str(), item.python_str()))
                    .collect(),
            ),
            _ => return parser.error("must define 'settings' as a mapping when set"),
        },
    };
    let replay_settings: Option<Vec<(String, String)>> =
        parser.execution_settings(field(header, "execution_settings"))?;
    let replay_anchor: String = match field(header, "replay_anchor") {
        None => REPLAY_ANCHORS[0].to_owned(),
        Some(value) => match value.as_str() {
            Some(anchor) if REPLAY_ANCHORS.contains(&anchor) => anchor.to_owned(),
            _ => return parser.error("must define 'replay_anchor' as 'auto' or 'never'"),
        },
    };
    let replay_on_change: Option<ReplayOnChange> =
        parser.replay_on_change(field(header, "replay_on_change"))?;
    let bounded_replay_fallback: Option<String> = match field(header, "bounded_replay_fallback") {
        None => None,
        Some(value) => match value.as_str() {
            Some(fallback) if BOUNDED_REPLAY_FALLBACKS.contains(&fallback) => {
                Some(fallback.to_owned())
            }
            _ => {
                return parser.error(format!(
                    "has unsupported bounded_replay_fallback '{}'",
                    value.python_str()
                ));
            }
        },
    };
    Ok(ModelSpec::Table(
        common,
        Box::new(TableSpec {
            source,
            engine,
            order_by,
            partition_by,
            ttl,
            retention,
            settings,
            replay_settings,
            replay_anchor,
            replay_on_change,
            bounded_replay_fallback,
        }),
    ))
}

fn discovery(message: String) -> SpecError {
    SpecError::new(ErrorKind::PipelineDiscovery, message)
}

fn field<'v>(mapping: &'v Value, key: &str) -> Option<&'v Value> {
    mapping.get(key).filter(|value| !value.is_none())
}

impl Parser<'_> {
    fn error<T>(&self, message: impl std::fmt::Display) -> SpecResult<T> {
        Err(discovery(format!(
            "MODEL(...) in '{}' {message}",
            self.file_path
        )))
    }

    fn optional_string(&self, header: &Value, key: &str) -> SpecResult<Option<String>> {
        match field(header, key) {
            None => Ok(None),
            Some(value) => match value.as_str() {
                Some(text) if !text.is_empty() => Ok(Some(text.to_owned())),
                _ => self.error(format!("must define '{key}' as a string when set")),
            },
        }
    }

    fn audits(&self, value: Option<&Value>, label: &str) -> SpecResult<Vec<usize>> {
        match value.map(|value| &value.kind) {
            None => Ok(Vec::new()),
            Some(Kind::List(items)) => Ok(items.iter().map(|item| item.handle).collect()),
            Some(_) => self.error(format!(
                "must define '{label}' as a bracketed list when set"
            )),
        }
    }

    fn columns(&self, value: Option<&Value>) -> SpecResult<Vec<ColumnSpec>> {
        let Some(value) = value else {
            return Ok(Vec::new());
        };
        let Kind::Dict(entries) = &value.kind else {
            return self.error("must define 'columns' as a parenthesized mapping");
        };
        entries
            .iter()
            .map(|(name, spec)| {
                let name: String = name.python_str();
                if !matches!(spec.kind, Kind::Dict(_)) {
                    return self.error(format!("column '{name}' must use a parenthesized mapping"));
                }
                let mut unknown: Vec<String> = spec
                    .mapping_entries()
                    .unwrap_or_default()
                    .iter()
                    .filter(|(key, _)| !key.as_str().is_some_and(|key| COLUMN_KEYS.contains(&key)))
                    .map(|(key, _)| key.python_str())
                    .collect();
                if !unknown.is_empty() {
                    unknown.sort_unstable();
                    return self.error(format!(
                        "column '{name}' has unknown keys: {}",
                        unknown.join(", ")
                    ));
                }
                let description: Option<String> = match field(spec, "description") {
                    None => None,
                    Some(description) => match description.as_str() {
                        Some(text) if !text.is_empty() => Some(text.to_owned()),
                        _ => {
                            return self.error(format!(
                                "column '{name}' must define 'description' as a string when set"
                            ));
                        }
                    },
                };
                let audits: Vec<usize> =
                    self.audits(field(spec, "audits"), &format!("columns.{name}.audits"))?;
                Ok(ColumnSpec {
                    name,
                    description,
                    audits,
                })
            })
            .collect()
    }

    fn execution_settings(
        &self,
        value: Option<&Value>,
    ) -> SpecResult<Option<Vec<(String, String)>>> {
        let Some(value) = value else {
            return Ok(None);
        };
        if !matches!(value.kind, Kind::Dict(_)) {
            return self.error("must define 'execution_settings' as a mapping");
        }
        let mut unknown: Vec<String> = value
            .mapping_entries()
            .unwrap_or_default()
            .iter()
            .filter(|(key, _)| {
                !key.as_str()
                    .is_some_and(|key| EXECUTION_SETTINGS_KEYS.contains(&key))
            })
            .map(|(key, _)| key.python_str())
            .collect();
        if !unknown.is_empty() {
            unknown.sort_unstable();
            return self.error(format!(
                "contains unsupported execution_settings keys: {}",
                unknown.join(", ")
            ));
        }
        let Some(replay) = field(value, "replay") else {
            return Ok(None);
        };
        let entries: &[(Value, Value)] = match &replay.kind {
            Kind::Dict(entries) if !entries.is_empty() => entries,
            _ => {
                return self
                    .error("must define 'execution_settings.replay' as a non-empty mapping");
            }
        };
        let mut invalid: Vec<String> = entries
            .iter()
            .filter(|(name, _)| !name.as_str().is_some_and(is_identifier))
            .map(|(name, _)| name.python_str())
            .collect();
        if !invalid.is_empty() {
            invalid.sort_unstable();
            return self.error(format!(
                "contains invalid execution_settings.replay setting names: {}",
                invalid.join(", ")
            ));
        }
        Ok(Some(
            entries
                .iter()
                .map(|(name, setting)| (name.python_str(), setting.python_str()))
                .collect(),
        ))
    }

    fn replay_on_change(&self, value: Option<&Value>) -> SpecResult<Option<ReplayOnChange>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let entries: &[(Value, Value)] = match &value.kind {
            Kind::Dict(entries) if entries.iter().all(|(key, _)| key.as_str().is_some()) => entries,
            _ => return self.error("must define 'replay_on_change' as a mapping when set"),
        };
        let mut unknown: Vec<&str> = entries
            .iter()
            .filter_map(|(key, _)| key.as_str())
            .filter(|key| !REPLAY_ON_CHANGE_KEYS.contains(key))
            .collect();
        if !unknown.is_empty() {
            unknown.sort_unstable();
            return self.error(format!(
                "contains unsupported replay_on_change keys: {}",
                unknown.join(", ")
            ));
        }
        Ok(Some(ReplayOnChange {
            breaking: self.replay_rule(field(value, "breaking"), "breaking")?,
            non_breaking: self.replay_rule(field(value, "non_breaking"), "non_breaking")?,
        }))
    }

    fn replay_rule(&self, value: Option<&Value>, key: &str) -> SpecResult<Option<ReplayRule>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let text: &str = match value.as_str() {
            Some(text) if !text.is_empty() => python_strip(text),
            _ => return self.error(format!("must define replay_on_change.{key} as a string")),
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
}
