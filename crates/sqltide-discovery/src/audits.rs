//! SQL audits: the fields an `AUDIT()` header sets, the generic audit instances a model header
//! declares, the parameters a generic definition uses, and the query an instance renders.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use sqltide_sqlanalysis::{SqlDialect, render_string_literal};

use crate::durations::parse_duration_seconds;
use crate::text::python_strip;
use crate::value::{Kind, Value};

const SEVERITY_KEY: &str = "severity";
const DESCRIPTION_KEY: &str = "description";
const NAME_KEY: &str = "name";
const EVERY_KEY: &str = "every";
const WARMUP_KEY: &str = "warmup";
const SCHEDULED_KEY: &str = "scheduled";
const DEFAULT_SEVERITY: &str = "error";
const ALLOWED_SEVERITIES: [&str; 2] = ["error", "warning"];
const LITERAL_DIALECT: &str = "clickhouse";
const ALLOWED_HEADER_KEYS: [&str; 6] = [
    SEVERITY_KEY,
    DESCRIPTION_KEY,
    NAME_KEY,
    EVERY_KEY,
    WARMUP_KEY,
    SCHEDULED_KEY,
];

static QUOTED_PARAMETER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"@'([A-Za-z_][A-Za-z0-9_]*)'").expect("valid quoted parameter pattern")
});
static RAW_PARAMETER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"@([A-Za-z_][A-Za-z0-9_]*)").expect("valid raw parameter pattern")
});

/// One generic audit a model or column header declares, with its remaining arguments.
#[derive(Debug, Clone, PartialEq)]
pub struct Instance<'a> {
    pub definition_name: String,
    pub arguments: Vec<(String, &'a Value)>,
    pub name: String,
    pub severity: String,
    pub description: Option<String>,
    pub severity_is_explicit: bool,
    pub cadence_seconds: Option<u64>,
    pub warmup_seconds: Option<u64>,
    pub scheduled: Option<bool>,
}

/// Why a generic audit instance could not be rendered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RenderError {
    /// An invalid instance, with its message.
    Invalid(String),
    /// A string the dialect could not render as a literal.
    Literal(String),
}

/// The settings one concrete `AUDIT()` header declares.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeaderFields {
    pub severity: String,
    pub description: Option<String>,
    pub name: Option<String>,
    pub severity_is_explicit: bool,
    pub cadence_seconds: Option<u64>,
    pub warmup_seconds: Option<u64>,
    pub scheduled: Option<bool>,
}

/// The message for the keys of a parsed `AUDIT()` header that it does not support, sorted.
#[must_use]
pub fn unsupported_audit_header_keys(
    file_path: &str,
    header: &[(String, &Value)],
) -> Option<String> {
    let mut unknown: Vec<&str> = header
        .iter()
        .map(|(key, _)| key.as_str())
        .filter(|key| !ALLOWED_HEADER_KEYS.contains(key))
        .collect();
    unknown.sort_unstable();
    (!unknown.is_empty()).then(|| {
        format!(
            "AUDIT() in '{file_path}' contains unsupported keys: {}",
            unknown.join(", ")
        )
    })
}

/// The settings of a parsed `AUDIT()` header whose keys are all supported.
///
/// # Errors
///
/// Returns the first invalid setting's message, checking severity, description, name, every,
/// warmup, then scheduled.
pub fn audit_header_fields(
    file_path: &str,
    header: &[(String, &Value)],
) -> Result<HeaderFields, String> {
    let prefix: String = format!("AUDIT() in '{file_path}'");
    let get = |key: &str| {
        header
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| *value)
    };
    let severity: String = match get(SEVERITY_KEY) {
        None => DEFAULT_SEVERITY.to_owned(),
        Some(value) => match value.as_str() {
            Some(text) if ALLOWED_SEVERITIES.contains(&text) => text.to_owned(),
            _ => {
                return Err(format!(
                    "{prefix} must define severity as 'error' or 'warning'"
                ));
            }
        },
    };
    let text = |key: &str| -> Result<Option<String>, String> {
        let Some(value) = get(key).filter(|value| !value.is_none()) else {
            return Ok(None);
        };
        match value.as_str().map(python_strip) {
            Some(text) if !text.is_empty() => Ok(Some(text.to_owned())),
            _ => Err(format!(
                "{prefix} must define {key} as a non-empty string when set"
            )),
        }
    };
    let description: Option<String> = text(DESCRIPTION_KEY)?;
    let name: Option<String> = text(NAME_KEY)?;
    let duration = |key: &str, allow_zero: bool| -> Result<Option<u64>, String> {
        get(key)
            .map(|value| {
                parse_duration_seconds(value.as_str(), &format!("{prefix} {key}"), allow_zero)
                    .map_err(|error| error.message)
            })
            .transpose()
    };
    let cadence_seconds: Option<u64> = duration(EVERY_KEY, false)?;
    let warmup_seconds: Option<u64> = duration(WARMUP_KEY, true)?;
    let scheduled: Option<bool> = match get(SCHEDULED_KEY).filter(|value| !value.is_none()) {
        None => None,
        Some(Value {
            kind: Kind::Bool(flag),
            ..
        }) => Some(*flag),
        Some(_) => return Err(format!("{prefix} scheduled must be a boolean")),
    };
    if scheduled == Some(false) && get(EVERY_KEY).is_some() {
        return Err(format!(
            "{prefix} cannot define both scheduled false and every"
        ));
    }
    Ok(HeaderFields {
        severity,
        description,
        name,
        severity_is_explicit: get(SEVERITY_KEY).is_some(),
        cadence_seconds,
        warmup_seconds,
        scheduled,
    })
}

/// The generic audits `raw_audits` declares, numbered from one, each named after
/// `default_name_prefix` unless it sets a name.
///
/// # Errors
///
/// Returns the first invalid audit entry's message, in the order the entries are checked.
pub fn generic_audit_instances<'a>(
    file_path: &str,
    raw_audits: &'a Value,
    implicit_arguments: &[(String, &'a Value)],
    default_name_prefix: &str,
) -> Result<Vec<Instance<'a>>, String> {
    let entries: &[Value] = match &raw_audits.kind {
        Kind::None => return Ok(Vec::new()),
        Kind::List(entries) if entries.is_empty() => return Ok(Vec::new()),
        Kind::List(entries) => entries,
        _ => return Err(format!("Model '{file_path}' must define audits as a list")),
    };
    let entry_error: String =
        format!("Model '{file_path}' audit items must be a string or single-key mapping");
    let mut instances: Vec<Instance<'a>> = Vec::with_capacity(entries.len());
    for (position, entry) in entries.iter().enumerate() {
        let (definition_name, explicit): (&str, Vec<(String, &'a Value)>) = match &entry.kind {
            Kind::Str(definition_name) => (definition_name, Vec::new()),
            Kind::Dict(pairs) if pairs.iter().all(|(key, _)| key.as_str().is_some()) => {
                let [(key, arguments)] = pairs.as_slice() else {
                    return Err(entry_error);
                };
                let definition_name: &str = key.as_str().unwrap_or_default();
                let explicit: Vec<(String, &'a Value)> = match &arguments.kind {
                    Kind::None => Vec::new(),
                    Kind::Dict(arguments)
                        if arguments.iter().all(|(key, _)| key.as_str().is_some()) =>
                    {
                        arguments
                            .iter()
                            .map(|(key, value)| {
                                (key.as_str().unwrap_or_default().to_owned(), value)
                            })
                            .collect()
                    }
                    _ => {
                        return Err(format!(
                            "Model '{file_path}' audit '{definition_name}' must define arguments \
                             as a mapping"
                        ));
                    }
                };
                (definition_name, explicit)
            }
            _ => return Err(entry_error),
        };
        if definition_name.is_empty() {
            return Err(format!(
                "Model '{file_path}' must use non-empty audit names under audits"
            ));
        }
        let prefix: String = format!("Model '{file_path}' audit '{definition_name}'");
        let mut arguments: Vec<(String, &'a Value)> = implicit_arguments.to_vec();
        for (key, value) in &explicit {
            match arguments.iter_mut().find(|(name, _)| name == key) {
                Some((_, implicit)) => {
                    if !python_equal(implicit, value) {
                        return Err(format!(
                            "{prefix} must not override implicit {key} from its header context"
                        ));
                    }
                    *implicit = value;
                }
                None => arguments.push((key.clone(), value)),
            }
        }
        let severity_is_explicit: bool = explicit.iter().any(|(key, _)| key == SEVERITY_KEY);
        let severity: String = match take(&mut arguments, SEVERITY_KEY) {
            None => DEFAULT_SEVERITY.to_owned(),
            Some(value) => match value.as_str() {
                Some(text) if ALLOWED_SEVERITIES.contains(&text) => text.to_owned(),
                _ => {
                    return Err(format!(
                        "{prefix} must define {SEVERITY_KEY} as one of: {}",
                        ALLOWED_SEVERITIES.join(", ")
                    ));
                }
            },
        };
        let description: Option<String> = optional_text(&mut arguments, DESCRIPTION_KEY, &prefix)?;
        let name: Option<String> = optional_text(&mut arguments, NAME_KEY, &prefix)?;
        let cadence_seconds: Option<u64> = duration(&mut arguments, EVERY_KEY, &prefix, false)?;
        let warmup_seconds: Option<u64> = duration(&mut arguments, WARMUP_KEY, &prefix, true)?;
        let scheduled: Option<bool> = match take(&mut arguments, SCHEDULED_KEY) {
            None => None,
            Some(Value {
                kind: Kind::Bool(flag),
                ..
            }) => Some(*flag),
            Some(_) => return Err(format!("{prefix} must define {SCHEDULED_KEY} as a boolean")),
        };
        if scheduled == Some(false) && cadence_seconds.is_some() {
            return Err(format!(
                "{prefix} cannot define both scheduled false and every"
            ));
        }
        instances.push(Instance {
            definition_name: definition_name.to_owned(),
            arguments,
            name: name.unwrap_or_else(|| {
                format!("{default_name_prefix}.{definition_name}.{}", position + 1)
            }),
            severity,
            description,
            severity_is_explicit,
            cadence_seconds,
            warmup_seconds,
            scheduled,
        });
    }
    Ok(instances)
}

/// The raw `@name` and quoted `@'name'` parameters of a definition query, each in first-use
/// order without repeats.
#[must_use]
pub fn generic_audit_parameter_names(query: &str) -> (Vec<String>, Vec<String>) {
    let names = |pattern: &Regex| -> Vec<String> {
        let mut seen: HashSet<&str> = HashSet::new();
        pattern
            .captures_iter(query)
            .filter_map(|captures| captures.get(1).map(|name| name.as_str()))
            .filter(|name| seen.insert(name))
            .map(str::to_owned)
            .collect()
    };
    (names(&RAW_PARAMETER), names(&QUOTED_PARAMETER))
}

/// The concrete query of one instance: every quoted parameter replaced by its string literal,
/// then every raw parameter by its text.
///
/// # Errors
///
/// Returns a missing, unsupported, or ill-typed argument, or a string the dialect could not
/// render.
pub fn render_generic_audit(
    file_path: &str,
    definition_name: &str,
    query: &str,
    raw_parameter_names: &[String],
    quoted_parameter_names: &[String],
    arguments: &[(String, &Value)],
) -> Result<String, RenderError> {
    let mut required: Vec<&str> = Vec::new();
    for name in raw_parameter_names.iter().chain(quoted_parameter_names) {
        if !required.contains(&name.as_str()) {
            required.push(name);
        }
    }
    let argument = |name: &str| {
        arguments
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| *value)
    };
    let missing: Vec<&str> = required
        .iter()
        .copied()
        .filter(|name| argument(name).is_none())
        .collect();
    if !missing.is_empty() {
        return Err(RenderError::Invalid(format!(
            "Model '{file_path}' is missing arguments for generic audit '{definition_name}': {}",
            missing.join(", ")
        )));
    }
    let mut unknown: Vec<&str> = arguments
        .iter()
        .map(|(key, _)| key.as_str())
        .filter(|key| !required.contains(key))
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(RenderError::Invalid(format!(
            "Model '{file_path}' has unsupported arguments for generic audit \
             '{definition_name}': {}",
            unknown.join(", ")
        )));
    }
    let dialect: SqlDialect = SqlDialect::new(LITERAL_DIALECT);
    let mut rendered: String = query.to_owned();
    for name in quoted_parameter_names {
        let value: &Value = argument(name).expect("every required argument is present");
        let replacement: String = quoted_argument(value, file_path, name, &dialect)?;
        rendered = rendered.replace(&format!("@'{name}'"), &replacement);
    }
    for name in raw_parameter_names {
        let value: &Value = argument(name).expect("every required argument is present");
        let replacement: String = raw_argument(value, file_path, name)?;
        rendered = rendered.replace(&format!("@{name}"), &replacement);
    }
    Ok(rendered)
}

/// Remove `key` from `arguments`, keeping the order of the rest.
fn take<'a>(arguments: &mut Vec<(String, &'a Value)>, key: &str) -> Option<&'a Value> {
    let position: usize = arguments.iter().position(|(name, _)| name == key)?;
    Some(arguments.remove(position).1)
}

fn optional_text(
    arguments: &mut Vec<(String, &Value)>,
    key: &str,
    prefix: &str,
) -> Result<Option<String>, String> {
    let Some(value) = take(arguments, key).filter(|value| !value.is_none()) else {
        return Ok(None);
    };
    match value.as_str().map(python_strip) {
        Some(text) if !text.is_empty() => Ok(Some(text.to_owned())),
        _ => Err(format!("{prefix} must define {key} as a non-empty string")),
    }
}

fn duration(
    arguments: &mut Vec<(String, &Value)>,
    key: &str,
    prefix: &str,
    allow_zero: bool,
) -> Result<Option<u64>, String> {
    let Some(value) = take(arguments, key) else {
        return Ok(None);
    };
    parse_duration_seconds(value.as_str(), &format!("{prefix} {key}"), allow_zero)
        .map(Some)
        .map_err(|error| error.message)
}

/// Python `==` between a header argument and an implicit string argument.
fn python_equal(left: &Value, right: &Value) -> bool {
    matches!((&left.kind, &right.kind), (Kind::Str(left), Kind::Str(right)) if left == right)
}

/// Python `str()` of a raw argument item: a string, an integer (including `bool`), or a float.
fn raw_text(value: &Value) -> Option<String> {
    match &value.kind {
        Kind::Str(_) | Kind::Bool(_) | Kind::Int(..) | Kind::Float(..) => Some(value.python_str()),
        _ => None,
    }
}

fn raw_argument(value: &Value, file_path: &str, name: &str) -> Result<String, RenderError> {
    if let Kind::List(items) = &value.kind {
        let texts: Option<Vec<String>> = items.iter().map(raw_text).collect();
        return texts.map(|texts| texts.join(", ")).ok_or_else(|| {
            RenderError::Invalid(format!(
                "Generic SQL audit arg '{name}' in '{file_path}' must be a list of strings or \
                 numbers"
            ))
        });
    }
    raw_text(value).ok_or_else(|| {
        RenderError::Invalid(format!(
            "Generic SQL audit arg '{name}' in '{file_path}' must be a string, number, or list"
        ))
    })
}

fn quoted_argument(
    value: &Value,
    file_path: &str,
    name: &str,
    dialect: &SqlDialect,
) -> Result<String, RenderError> {
    let literal = |text: &str| {
        render_string_literal(text, dialect).map_err(|_| RenderError::Literal(text.to_owned()))
    };
    match &value.kind {
        Kind::List(items) => {
            let texts: Option<Vec<&str>> = items.iter().map(Value::as_str).collect();
            let Some(texts) = texts else {
                return Err(RenderError::Invalid(format!(
                    "Generic SQL audit arg '{name}' in '{file_path}' must be a list of strings"
                )));
            };
            texts
                .into_iter()
                .map(literal)
                .collect::<Result<Vec<String>, RenderError>>()
                .map(|literals| literals.join(", "))
        }
        Kind::Str(text) => literal(text),
        _ => Err(RenderError::Invalid(format!(
            "Generic SQL audit arg '{name}' in '{file_path}' must be a string or list of strings"
        ))),
    }
}
