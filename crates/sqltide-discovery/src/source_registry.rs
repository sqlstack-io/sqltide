//! The `sources/*.yml` schema: Kafka landing sources, adopted stream tables, and Postgres
//! refresh sources, with interpolated string fields and project-wide uniqueness checks.
//!
//! Deriving a Kafka source name runs a user macro, so the caller supplies it.

use std::collections::{BTreeMap, HashMap};

use crate::INTERPOLATION_TOKEN_START;
use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::freshness::{Freshness, parse_freshness_policy};
use crate::interpolation::{self, Environment};
use crate::retention::{KafkaRetention, Retention, is_identifier, parse_kafka_retention};
use crate::text::{decimal_digit, is_python_space, python_strip};
use crate::value::{Kind, Value, from_interpolation, index};

const SOURCE_FILE_KEYS: [&str; 1] = ["sources"];
const SOURCE_KEYS: [&str; 20] = [
    "name",
    "kind",
    "broker_list",
    "topic",
    "consumer_group",
    "format",
    "ttl",
    "retention",
    "settings",
    "table_name",
    "replay_boundary",
    "freshness",
    "host",
    "port",
    "database",
    "table",
    "user",
    "password_env",
    "refresh",
    "append",
];
const SOURCE_KINDS: [&str; 3] = ["kafka", "stream_table", "postgres"];
const KAFKA_KIND: &str = "kafka";
const POSTGRES_KIND: &str = "postgres";
const POSTGRES_REQUIRED_KEYS: [&str; 4] = ["host", "database", "table", "user"];
const POSTGRES_FORBIDDEN_KEYS: [&str; 9] = [
    "broker_list",
    "topic",
    "consumer_group",
    "format",
    "ttl",
    "retention",
    "settings",
    "table_name",
    "replay_boundary",
];
const ADOPTED_FORBIDDEN_KEYS: [&str; 7] = [
    "broker_list",
    "topic",
    "consumer_group",
    "format",
    "ttl",
    "retention",
    "settings",
];
const REPLAY_BOUNDARY_KEYS: [&str; 2] = ["mode", "columns"];
const REPLAY_BOUNDARY_MODES: [&str; 4] = ["offsets", "timestamp", "landed_at", "cursor"];
const KAFKA_BOUNDARY_MODES: [&str; 3] = ["landed_at", "offsets", "timestamp"];
const ADOPTED_BOUNDARY_MODES: [&str; 3] = ["cursor", "offsets", "timestamp"];
/// Boundary role columns, in the order `BoundaryColumns` stores them.
const BOUNDARY_COLUMN_KEYS: [&str; 5] = [
    "_replay_partition",
    "_replay_offset",
    "_replay_timestamp",
    "_replay_landed_at",
    "_replay_cursor",
];
const REFRESH_UNITS: [&str; 4] = ["SECOND", "MINUTE", "HOUR", "DAY"];
const TRUE_LITERALS: [&str; 3] = ["true", "yes", "1"];
const FALSE_LITERALS: [&str; 3] = ["false", "no", "0"];
pub const DEFAULT_KAFKA_FORMAT: &str = "JSONAsString";
pub const DEFAULT_POSTGRES_PORT: i128 = 5432;

/// One source file: its path and decoded payload, or the message of its decoding error.
pub struct SourceFile {
    pub path: String,
    pub payload: Result<Value, String>,
}

/// Project defaults managed Kafka sources inherit.
pub struct KafkaDefaults {
    pub ttl: Option<String>,
    /// Whether the project sets a managed source retention policy.
    pub retention_set: bool,
    pub broker_list: Option<String>,
}

/// A derived Kafka source name: `(name, macro name, macro fingerprint)`.
pub type DerivedName = (String, String, String);

/// Derive a source name from `(topic, label, file path)` by running the naming macro.
pub type DeriveName<'a> = dyn FnMut(&str, &str, &str) -> SpecResult<DerivedName> + 'a;

/// Replay boundary role columns: partition, offset, timestamp, landed-at, cursor.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BoundaryColumns(pub [Option<String>; 5]);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Boundary {
    pub mode: String,
    pub columns: BoundaryColumns,
}

/// Where a Kafka source's retention comes from.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KafkaRetentionChoice {
    /// Disabled or a policy authored on the source.
    Source(Retention<KafkaRetention>),
    /// The project's default policy.
    Project,
    None,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KafkaSource {
    pub name: String,
    pub derived: Option<(String, String)>,
    pub topic: String,
    pub broker_list: String,
    pub consumer_group: Option<String>,
    pub format: String,
    pub ttl: Option<String>,
    pub retention: KafkaRetentionChoice,
    pub settings: Vec<(String, String)>,
    pub replay_boundary: Boundary,
}

/// A Postgres port: a number, or an authored integer passed through by handle.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Port {
    Number(i128),
    Handle(usize),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PostgresSource {
    pub name: String,
    pub host: String,
    pub database: String,
    pub table: String,
    pub user: String,
    pub refresh: String,
    pub port: Port,
    pub password_env: Option<String>,
    pub append: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SourceKind {
    Kafka(Box<KafkaSource>),
    Adopted {
        name: String,
        table_name: String,
        replay_boundary: Boundary,
    },
    Postgres(PostgresSource),
}

/// One source; `freshness` is `None` when the project default applies.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Source {
    pub kind: SourceKind,
    pub freshness: Option<Freshness>,
}

impl Source {
    fn name(&self) -> &str {
        match &self.kind {
            SourceKind::Kafka(source) => &source.name,
            SourceKind::Adopted { name, .. } => name,
            SourceKind::Postgres(source) => &source.name,
        }
    }
}

/// Parse every source file in order, then check names are unique and adopted tables agree.
///
/// # Errors
///
/// Returns the first problem: a discovery error, a configuration error from interpolation, or
/// the naming macro's error.
pub fn parse_source_registry(
    files: &[SourceFile],
    variables: &Value,
    environment: &dyn Environment,
    defaults: &KafkaDefaults,
    derive_name: &mut DeriveName<'_>,
) -> SpecResult<Vec<Vec<Source>>> {
    let mut objects: HashMap<usize, &Value> = HashMap::new();
    index(variables, &mut objects);
    for file in files {
        if let Ok(payload) = &file.payload {
            index(payload, &mut objects);
        }
    }
    let mut registry: Registry<'_> = Registry {
        objects,
        variables: None,
        variables_value: variables,
        environment,
        defaults,
        derive_name,
    };
    let parsed: Vec<Vec<Source>> = files
        .iter()
        .map(|file| registry.file(file))
        .collect::<SpecResult<_>>()?;
    let mut paths_by_name: HashMap<&str, &str> = HashMap::new();
    for (file, sources) in files.iter().zip(&parsed) {
        for source in sources {
            if let Some(existing) = paths_by_name.insert(source.name(), &file.path) {
                return Err(discovery(format!(
                    "Duplicate source name '{}' found in '{existing}' and '{}'",
                    source.name(),
                    file.path
                )));
            }
        }
    }
    let mut boundaries: HashMap<&str, &Boundary> = HashMap::new();
    for source in parsed.iter().flatten() {
        if let SourceKind::Adopted {
            table_name,
            replay_boundary,
            ..
        } = &source.kind
            && boundaries
                .insert(table_name, replay_boundary)
                .is_some_and(|existing| existing != replay_boundary)
        {
            return Err(discovery(format!(
                "Adopted source table '{table_name}' has conflicting replay mappings"
            )));
        }
    }
    Ok(parsed)
}

fn discovery(message: String) -> SpecError {
    SpecError::new(ErrorKind::PipelineDiscovery, message)
}

struct Registry<'a> {
    objects: HashMap<usize, &'a Value>,
    variables: Option<SpecResult<BTreeMap<String, interpolation::Value>>>,
    variables_value: &'a Value,
    environment: &'a dyn Environment,
    defaults: &'a KafkaDefaults,
    derive_name: &'a mut DeriveName<'a>,
}

/// Parsing context for one source file.
struct FileContext<'p> {
    path: &'p str,
}

impl FileContext<'_> {
    fn error<T>(&self, message: impl std::fmt::Display) -> SpecResult<T> {
        Err(discovery(format!("Source file '{}' {message}", self.path)))
    }

    fn mapping<'v>(
        &self,
        value: Option<&'v Value>,
        field_path: &str,
        optional: bool,
    ) -> SpecResult<Option<&'v Value>> {
        let value: Option<&Value> = value.filter(|value| !value.is_none());
        let Some(value) = value else {
            return if optional {
                Ok(None)
            } else {
                self.error(format!("{field_path} must be a mapping"))
            };
        };
        let Kind::Dict(entries) = &value.kind else {
            return self.error(format!("{field_path} must be a mapping"));
        };
        let mut interpolated: Vec<&str> = Vec::new();
        for (key, _) in entries {
            let Some(key) = key.as_str() else {
                return self.error(format!("{field_path} must be a mapping"));
            };
            if key.contains(INTERPOLATION_TOKEN_START) {
                interpolated.push(key);
            }
        }
        if !interpolated.is_empty() {
            interpolated.sort_unstable();
            return self.error(format!(
                "{field_path} must not interpolate mapping keys: {}",
                interpolated.join(", ")
            ));
        }
        Ok(Some(value))
    }

    fn allowed_keys(&self, mapping: &Value, allowed: &[&str], field_path: &str) -> SpecResult<()> {
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
            "{field_path} contains unsupported keys: {}",
            unknown.join(", ")
        ))
    }
}

fn field<'v>(mapping: &'v Value, key: &str) -> Option<&'v Value> {
    mapping.get(key).filter(|value| !value.is_none())
}

impl Registry<'_> {
    fn interpolate(&mut self, value: &Value, field_path: &str) -> SpecResult<Value> {
        let variables_value: &Value = self.variables_value;
        let variables: &BTreeMap<String, interpolation::Value> = self
            .variables
            .get_or_insert_with(|| {
                variables_value
                    .mapping_entries()
                    .unwrap_or_default()
                    .iter()
                    .map(|(key, item)| Ok((key.python_str(), item.to_interpolation()?)))
                    .collect()
            })
            .as_ref()
            .map_err(Clone::clone)?;
        let interpolated: interpolation::Value = interpolation::interpolate_config_value(
            &value.to_interpolation()?,
            variables,
            self.environment,
            field_path,
        )
        .map_err(|message| SpecError::new(ErrorKind::ProjectConfig, message))?;
        Ok(from_interpolation(&self.objects, interpolated))
    }

    /// An interpolated string field, which must resolve to a non-empty string.
    fn string(
        &mut self,
        context: &FileContext<'_>,
        value: &Value,
        field_path: &str,
    ) -> SpecResult<String> {
        let interpolated: Value =
            self.interpolate(value, &format!("{}:{field_path}", context.path))?;
        match interpolated.as_str() {
            Some(text) if !text.is_empty() => Ok(text.to_owned()),
            _ => context.error(format!("{field_path} must resolve to a non-empty string")),
        }
    }

    fn optional_string(
        &mut self,
        context: &FileContext<'_>,
        mapping: &Value,
        key: &str,
        field_path: &str,
    ) -> SpecResult<Option<String>> {
        match field(mapping, key) {
            None => Ok(None),
            Some(value) => self
                .string(context, value, &format!("{field_path}.{key}"))
                .map(Some),
        }
    }

    fn required_string(
        &mut self,
        context: &FileContext<'_>,
        mapping: &Value,
        key: &str,
        field_path: &str,
    ) -> SpecResult<String> {
        match self.optional_string(context, mapping, key, field_path)? {
            Some(text) => Ok(text),
            None => context.error(format!("{field_path}.{key} must be a non-empty string")),
        }
    }

    fn file(&mut self, file: &SourceFile) -> SpecResult<Vec<Source>> {
        let context: FileContext<'_> = FileContext { path: &file.path };
        let payload: &Value = match &file.payload {
            Ok(payload) => payload,
            Err(message) => {
                return context.error(format!("contains invalid YAML: {message}"));
            }
        };
        let payload: &Value = context
            .mapping(Some(payload), "source file", false)?
            .unwrap_or(payload);
        context.allowed_keys(payload, &SOURCE_FILE_KEYS, "source file")?;
        let Some(Value {
            kind: Kind::List(sources),
            ..
        }) = payload.get("sources")
        else {
            return context.error("must define sources as a list");
        };
        sources
            .iter()
            .enumerate()
            .map(|(index, source)| self.source(&context, source, &format!("sources[{index}]")))
            .collect()
    }

    fn source(
        &mut self,
        context: &FileContext<'_>,
        value: &Value,
        label: &str,
    ) -> SpecResult<Source> {
        let mapping: &Value = context.mapping(Some(value), label, false)?.unwrap_or(value);
        context.allowed_keys(mapping, &SOURCE_KEYS, label)?;
        let kind: String = self.required_string(context, mapping, "kind", label)?;
        if !SOURCE_KINDS.contains(&kind.as_str()) {
            return context.error(format!(
                "{label}.kind must be one of 'kafka', 'stream_table', 'postgres'"
            ));
        }
        let freshness: Option<Freshness> = parse_freshness_policy(
            mapping.get("freshness"),
            &format!("{label}.freshness"),
            context.path,
        )?;
        if kind == KAFKA_KIND {
            let topic: String = self.required_string(context, mapping, "topic", label)?;
            let (name, derived) = match self.optional_string(context, mapping, "name", label)? {
                Some(name) => (name, None),
                None => {
                    let (name, macro_name, fingerprint) =
                        (self.derive_name)(&topic, label, context.path)?;
                    (name, Some((macro_name, fingerprint)))
                }
            };
            validate_name(context, &name, label)?;
            return Ok(Source {
                kind: SourceKind::Kafka(Box::new(
                    self.kafka(context, mapping, label, name, derived, topic)?,
                )),
                freshness,
            });
        }
        let name: String = self.required_string(context, mapping, "name", label)?;
        validate_name(context, &name, label)?;
        let kind: SourceKind = if kind == POSTGRES_KIND {
            SourceKind::Postgres(self.postgres(context, mapping, label, name)?)
        } else {
            self.adopted(context, mapping, label, name)?
        };
        Ok(Source { kind, freshness })
    }

    fn kafka(
        &mut self,
        context: &FileContext<'_>,
        mapping: &Value,
        label: &str,
        name: String,
        derived: Option<(String, String)>,
        topic: String,
    ) -> SpecResult<KafkaSource> {
        if field(mapping, "table_name").is_some() {
            return context.error(format!(
                "{label} must not mix Kafka and adopted table fields"
            ));
        }
        let replay_boundary: Boundary = self.boundary(
            context,
            mapping.get("replay_boundary"),
            &format!("{label}.replay_boundary"),
            &KAFKA_BOUNDARY_MODES,
            false,
        )?;
        let settings_label: String = format!("{label}.settings");
        let mut settings: Vec<(String, String)> = Vec::new();
        if let Some(settings_mapping) =
            context.mapping(mapping.get("settings"), &settings_label, true)?
        {
            for (key, value) in settings_mapping.mapping_entries().unwrap_or_default() {
                let key: String = key.python_str();
                let text: String =
                    self.string(context, value, &format!("{settings_label}.{key}"))?;
                settings.push((key, text));
            }
        }
        let broker_list: String = match self.optional_string(
            context,
            mapping,
            "broker_list",
            label,
        )? {
            Some(broker_list) => broker_list,
            None => match &self.defaults.broker_list {
                Some(broker_list) => broker_list.clone(),
                None => {
                    return context.error(format!(
                        "{label}.broker_list must be a non-empty string or defaults.kafka_broker_list must be configured"
                    ));
                }
            },
        };
        let explicit_ttl: Option<String> = self.optional_string(context, mapping, "ttl", label)?;
        let retention_path: String = format!("Source file '{}' {label}.retention", context.path);
        let explicit_retention: Retention<KafkaRetention> = match mapping.get("retention") {
            None => Retention::Unset,
            Some(value) => self
                .interpolate(value, &retention_path)
                .and_then(|value| parse_kafka_retention(Some(&value), &retention_path))
                .map_err(|failure| match failure.kind {
                    ErrorKind::Type => failure,
                    _ => failure.reraise(ErrorKind::PipelineDiscovery),
                })?,
        };
        let retention_set: bool = !matches!(explicit_retention, Retention::Unset);
        if explicit_ttl.is_some() && retention_set {
            return context.error(format!("{label} cannot combine ttl with typed retention"));
        }
        let consumer_group: Option<String> =
            self.optional_string(context, mapping, "consumer_group", label)?;
        let format: String = self
            .optional_string(context, mapping, "format", label)?
            .unwrap_or_else(|| DEFAULT_KAFKA_FORMAT.to_owned());
        let explicit_ttl_set: bool = explicit_ttl.is_some();
        let ttl: Option<String> = match explicit_ttl {
            Some(ttl) => Some(ttl),
            None if !retention_set => self.defaults.ttl.clone(),
            None => None,
        };
        let retention: KafkaRetentionChoice = if retention_set {
            KafkaRetentionChoice::Source(explicit_retention)
        } else if explicit_ttl_set || !self.defaults.retention_set {
            KafkaRetentionChoice::None
        } else {
            KafkaRetentionChoice::Project
        };
        Ok(KafkaSource {
            name,
            derived,
            topic,
            broker_list,
            consumer_group,
            format,
            ttl,
            retention,
            settings,
            replay_boundary,
        })
    }

    fn postgres(
        &mut self,
        context: &FileContext<'_>,
        mapping: &Value,
        label: &str,
        name: String,
    ) -> SpecResult<PostgresSource> {
        let present: Vec<&str> = POSTGRES_FORBIDDEN_KEYS
            .into_iter()
            .filter(|key| mapping.get(key).is_some())
            .collect();
        if !present.is_empty() {
            return context.error(format!(
                "{label} is a Postgres source and must not declare streaming fields: {}",
                present.join(", ")
            ));
        }
        let mut required: Vec<String> = Vec::new();
        for key in POSTGRES_REQUIRED_KEYS {
            required.push(self.required_string(context, mapping, key, label)?);
        }
        let refresh: String = self.required_string(context, mapping, "refresh", label)?;
        if !is_refresh_interval(&refresh) {
            return context.error(format!(
                "{label}.refresh must be an interval such as '5 MINUTE' or '1 HOUR', not '{refresh}'"
            ));
        }
        let port: Port = postgres_port(context, field(mapping, "port"), label)?;
        let password_env: Option<String> =
            self.optional_string(context, mapping, "password_env", label)?;
        let append: bool = postgres_append(context, field(mapping, "append"), label)?;
        let [host, database, table, user]: [String; 4] = required
            .try_into()
            .unwrap_or_else(|_| unreachable!("four required keys"));
        Ok(PostgresSource {
            name,
            host,
            database,
            table,
            user,
            refresh,
            port,
            password_env,
            append,
        })
    }

    fn adopted(
        &mut self,
        context: &FileContext<'_>,
        mapping: &Value,
        label: &str,
        name: String,
    ) -> SpecResult<SourceKind> {
        let present: Vec<&str> = ADOPTED_FORBIDDEN_KEYS
            .into_iter()
            .filter(|key| field(mapping, key).is_some())
            .collect();
        if !present.is_empty() {
            return context.error(format!(
                "{label} must not mix adopted and Kafka fields: {}",
                present.join(", ")
            ));
        }
        let replay_boundary: Boundary = self.boundary(
            context,
            mapping.get("replay_boundary"),
            &format!("{label}.replay_boundary"),
            &ADOPTED_BOUNDARY_MODES,
            true,
        )?;
        let table_name: String = self.required_string(context, mapping, "table_name", label)?;
        Ok(SourceKind::Adopted {
            name,
            table_name,
            replay_boundary,
        })
    }

    fn boundary(
        &mut self,
        context: &FileContext<'_>,
        value: Option<&Value>,
        label: &str,
        allowed: &[&str],
        adopted: bool,
    ) -> SpecResult<Boundary> {
        let mapping: &Value = context
            .mapping(value, label, false)?
            .unwrap_or_else(|| unreachable!("a required mapping"));
        context.allowed_keys(mapping, &REPLAY_BOUNDARY_KEYS, label)?;
        let mode: String = self.required_string(context, mapping, "mode", label)?;
        if !REPLAY_BOUNDARY_MODES.contains(&mode.as_str()) {
            return context.error(format!("{label}.mode is unsupported"));
        }
        if !allowed.contains(&mode.as_str()) {
            return context.error(format!(
                "{label}.mode '{mode}' must be one of: {}",
                allowed.join(", ")
            ));
        }
        let raw_columns: Option<&Value> = field(mapping, "columns");
        if !adopted && raw_columns.is_some() {
            return context.error(format!("{label}.columns is only valid for adopted sources"));
        }
        let columns_label: String = format!("{label}.columns");
        let mut columns: BoundaryColumns = BoundaryColumns::default();
        if let Some(columns_mapping) = context.mapping(raw_columns, &columns_label, true)? {
            context.allowed_keys(columns_mapping, &BOUNDARY_COLUMN_KEYS, &columns_label)?;
            for (slot, key) in columns.0.iter_mut().zip(BOUNDARY_COLUMN_KEYS) {
                *slot = self.optional_string(context, columns_mapping, key, &columns_label)?;
            }
        }
        if adopted {
            validate_adopted_columns(context, &mode, &columns, label)?;
        }
        Ok(Boundary { mode, columns })
    }
}

fn validate_name(context: &FileContext<'_>, name: &str, label: &str) -> SpecResult<()> {
    if is_identifier(name) {
        return Ok(());
    }
    context.error(format!(
        "{label}.name must resolve to an unqualified identifier using only letters, numbers, and underscores"
    ))
}

fn validate_adopted_columns(
    context: &FileContext<'_>,
    mode: &str,
    columns: &BoundaryColumns,
    label: &str,
) -> SpecResult<()> {
    let [partition, offset, timestamp, landed_at, cursor] = &columns.0;
    let set = |column: &Option<String>| column.is_some();
    match mode {
        "offsets" => {
            if !(set(partition) && set(offset) && set(timestamp)) {
                return context.error(format!(
                    "{label} offsets mode requires partition, offset, and timestamp role columns"
                ));
            }
            if set(cursor) || set(landed_at) {
                return context.error(format!(
                    "{label} offsets mode has incompatible role columns"
                ));
            }
        }
        "timestamp" => {
            if !set(timestamp) {
                return context.error(format!(
                    "{label} timestamp mode requires a timestamp role column"
                ));
            }
            if set(partition) || set(offset) || set(cursor) || set(landed_at) {
                return context.error(format!(
                    "{label} timestamp mode has incompatible role columns"
                ));
            }
        }
        _ => {
            if !(set(cursor) && set(timestamp)) {
                return context.error(format!(
                    "{label} cursor mode requires cursor and timestamp role columns"
                ));
            }
            if set(partition) || set(offset) || set(landed_at) {
                return context.error(format!("{label} cursor mode has incompatible role columns"));
            }
        }
    }
    Ok(())
}

fn postgres_port(
    context: &FileContext<'_>,
    value: Option<&Value>,
    label: &str,
) -> SpecResult<Port> {
    let Some(value) = value else {
        return Ok(Port::Number(DEFAULT_POSTGRES_PORT));
    };
    match &value.kind {
        Kind::Int(_, _) => return Ok(Port::Handle(value.handle)),
        Kind::Str(text) => {
            if let Some(port) = decimal_number(python_strip(text)) {
                return Ok(Port::Number(port));
            }
        }
        _ => {}
    }
    context.error(format!(
        "{label}.port must be a whole number, not '{}'",
        value.python_str()
    ))
}

/// A non-empty run of Unicode decimal digits as a number, `None` when it does not fit.
fn decimal_number(text: &str) -> Option<i128> {
    if text.is_empty() {
        return None;
    }
    text.chars().try_fold(0_i128, |total, character| {
        total
            .checked_mul(10)?
            .checked_add(i128::from(decimal_digit(character)?))
    })
}

fn postgres_append(
    context: &FileContext<'_>,
    value: Option<&Value>,
    label: &str,
) -> SpecResult<bool> {
    let Some(value) = value else {
        return Ok(true);
    };
    match &value.kind {
        Kind::Bool(flag) => return Ok(*flag),
        Kind::Str(text) => {
            let normalized: String = python_strip(text).to_lowercase();
            if TRUE_LITERALS.contains(&normalized.as_str()) {
                return Ok(true);
            }
            if FALSE_LITERALS.contains(&normalized.as_str()) {
                return Ok(false);
            }
        }
        _ => {}
    }
    context.error(format!(
        "{label}.append must be a boolean, not '{}'",
        value.python_str()
    ))
}

/// Python's `^\d+\s+(SECOND|MINUTE|HOUR|DAY)S?$` with `IGNORECASE`, matched from the start.
fn is_refresh_interval(text: &str) -> bool {
    let characters: Vec<char> = text.chars().collect();
    let digits: usize = characters
        .iter()
        .take_while(|&&c| decimal_digit(c).is_some())
        .count();
    let spaces: usize = characters[digits..]
        .iter()
        .take_while(|&&c| is_python_space(c))
        .count();
    if digits == 0 || spaces == 0 {
        return false;
    }
    let rest: &[char] = &characters[digits + spaces..];
    REFRESH_UNITS.iter().any(|unit| {
        let length: usize = unit.len();
        if rest.len() < length
            || !rest
                .iter()
                .zip(unit.chars())
                .all(|(&c, letter)| folds_to(c, letter))
        {
            return false;
        }
        let mut tail: &[char] = &rest[length..];
        if tail.first().is_some_and(|&c| folds_to(c, 'S')) {
            tail = &tail[1..];
        }
        tail.is_empty() || tail == ['\n']
    })
}

/// Whether `character` matches upper-case ASCII `letter` under Python's `IGNORECASE`.
fn folds_to(character: char, letter: char) -> bool {
    character.to_ascii_uppercase() == letter
        || (letter == 'I' && matches!(character, '\u{130}' | '\u{131}'))
        || (letter == 'S' && character == '\u{17f}')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_refresh_intervals_as_python_does() {
        let cases: [(&str, bool); 9] = [
            ("5 MINUTE", true),
            ("5 minutes\n", true),
            ("٥\u{1c}hour", true),
            ("5 ſecond", true),
            ("5 mınute", true),
            ("5 hours\n\n", false),
            ("5 minutes ", false),
            ("5MINUTE", false),
            ("x MINUTE", false),
        ];
        for (text, expected) in cases {
            assert_eq!(is_refresh_interval(text), expected, "{text:?}");
        }
    }
}
