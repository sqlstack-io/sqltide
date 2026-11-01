//! Project-wide `CONSTANT (...)` and `ENUM (...)` declarations under `constants/` and `enums/`:
//! parsing, validation, and ClickHouse literal rendering of `@const("name")` and
//! `@enum("name").MEMBER` references.
//!
//! The declaration rules follow Apache-2.0 SQLBuild's constants and enums at 6a6aeab7b.

use std::collections::BTreeMap;

use sqltide_sqlanalysis::{SqlDialect, render_string_literal};
use sqltide_sqltext::model_header::{HeaderValue, parse_declaration_header};
use sqltide_sqltext::sql_scan::{find_matching_parenthesis, skip_trivia};

use crate::errors::{ErrorKind, SpecError, SpecResult};

const CONSTANT_KIND: &str = "CONSTANT";
const ENUM_KIND: &str = "ENUM";
const DECLARATION_KINDS: [&str; 2] = [CONSTANT_KIND, ENUM_KIND];
const CONSTANT_KEYS: [&str; 4] = ["name", "value", "type", "render_as"];
const ENUM_KEYS: [&str; 2] = ["name", "members"];
const SCALAR_TYPES: [&str; 6] = ["string", "integer", "boolean", "float", "decimal", "null"];
const COLLECTION_TYPES: [&str; 3] = ["list", "set", "object"];
const MAX_DEPTH: usize = 32;
const MAX_ELEMENTS: usize = 10_000;
const DECIMAL128_PRECISION: usize = 38;
const DECIMAL256_PRECISION: usize = 76;
const CLICKHOUSE_DIALECT: &str = "clickhouse";

/// One typed constant value. Sets hold their values in canonical order; objects in key order.
#[derive(Clone, Debug, PartialEq)]
pub enum ConstantValue {
    Null,
    Boolean(bool),
    Integer(i64),
    Float(f64),
    /// Plain decimal notation, as authored.
    Decimal(String),
    String(String),
    List(Vec<ConstantValue>),
    Set(Vec<ConstantValue>),
    Object(Vec<(String, ConstantValue)>),
}

/// How a list or set constant renders: a parenthesized value list or a native array.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Rendering {
    ValueList,
    Array,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Constant {
    pub value: ConstantValue,
    pub rendering: Rendering,
}

/// An enum member's value: every member of one enum has the same kind.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum EnumValue {
    Text(String),
    Integer(i64),
}

/// Every public constant and enum of a project, by name.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Declarations {
    pub constants: BTreeMap<String, Constant>,
    pub enums: BTreeMap<String, Vec<(String, EnumValue)>>,
}

fn failure(message: impl Into<String>) -> SpecError {
    SpecError::new(ErrorKind::PipelineDiscovery, message)
}

/// Parse and validate `(path, contents)` declaration files from `constants/` and `enums/`.
///
/// # Errors
///
/// Returns the first malformed file, invalid declaration, or duplicate name.
pub fn load_declarations(
    constant_files: &[(String, String)],
    enum_files: &[(String, String)],
) -> SpecResult<Declarations> {
    let mut declarations: Declarations = Declarations::default();
    let mut constant_paths: BTreeMap<String, String> = BTreeMap::new();
    for (path, contents) in constant_files {
        for header in declaration_headers(contents, path, CONSTANT_KIND)? {
            let (name, constant) = constant(&header, path)?;
            if let Some(first) = constant_paths.insert(name.clone(), path.clone()) {
                return Err(failure(format!(
                    "Duplicate constant '{name}' declared in {first} and {path}"
                )));
            }
            declarations.constants.insert(name, constant);
        }
    }
    let mut enum_paths: BTreeMap<String, String> = BTreeMap::new();
    for (path, contents) in enum_files {
        for header in declaration_headers(contents, path, ENUM_KIND)? {
            let (name, members) = enum_declaration(&header, path)?;
            if let Some(first) = enum_paths.insert(name.clone(), path.clone()) {
                return Err(failure(format!(
                    "Duplicate enum '{name}' declared in {first} and {path}"
                )));
            }
            declarations.enums.insert(name, members);
        }
    }
    Ok(declarations)
}

/// Every `kind (...);` header of one file, parsed.
fn declaration_headers(
    contents: &str,
    path: &str,
    kind: &str,
) -> SpecResult<Vec<Vec<(String, HeaderValue)>>> {
    let text: Vec<char> = contents.chars().collect();
    let trivia_end = |start: usize| {
        skip_trivia(&text, start)
            .map_err(|error| failure(format!("{path} could not be scanned: {}", error.message)))
    };
    let mut headers: Vec<Vec<(String, HeaderValue)>> = Vec::new();
    let mut cursor: usize = trivia_end(0)?;
    while cursor < text.len() {
        let Some((actual, open)) = declaration_start(&text, cursor) else {
            return Err(failure(format!(
                "{path} must contain only {kind}(...) declarations"
            )));
        };
        if actual != kind {
            return Err(failure(format!(
                "{path} contains {actual}(...) under the {}s root",
                kind.to_ascii_lowercase()
            )));
        }
        let close: usize = find_matching_parenthesis(&text, open, kind)
            .map_err(|_| failure(format!("{path} has an unterminated declaration header")))?;
        cursor = trivia_end(close + 1)?;
        if text.get(cursor) != Some(&';') {
            return Err(failure(format!(
                "{kind}(...) in '{path}' must end with ';'"
            )));
        }
        let header: String = text[open + 1..close].iter().collect();
        headers.push(parse_declaration_header(&header).map_err(|error| {
            failure(format!(
                "{kind}(...) in '{path}' could not be parsed: {error}"
            ))
        })?);
        cursor = trivia_end(cursor + 1)?;
    }
    if headers.is_empty() {
        return Err(failure(format!(
            "{path} contains no {kind}(...) declarations"
        )));
    }
    Ok(headers)
}

/// `KIND\s*(` at `start`: the kind and the index of its parenthesis.
fn declaration_start(text: &[char], start: usize) -> Option<(&'static str, usize)> {
    DECLARATION_KINDS.into_iter().find_map(|kind| {
        let end: usize = start + kind.chars().count();
        if !text.get(start..end)?.iter().copied().eq(kind.chars()) {
            return None;
        }
        let open: usize = text[end..]
            .iter()
            .position(|character| !character.is_whitespace())
            .map_or(text.len(), |offset| end + offset);
        (text.get(open) == Some(&'(')).then_some((kind, open))
    })
}

fn get<'a>(header: &'a [(String, HeaderValue)], key: &str) -> Option<&'a HeaderValue> {
    header
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn unknown_keys(header: &[(String, HeaderValue)], known: &[&str]) -> Option<String> {
    let mut unknown: Vec<&str> = header
        .iter()
        .map(|(key, _)| key.as_str())
        .filter(|key| !known.contains(key))
        .collect();
    unknown.sort_unstable();
    (!unknown.is_empty()).then(|| unknown.join(", "))
}

/// `^[A-Za-z_][A-Za-z0-9_]*$`.
fn is_identifier(text: &str) -> bool {
    let mut characters = text.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|character| character.is_ascii_alphanumeric() || character == '_')
}

/// A bare word or quoted string used as text.
fn text(value: &HeaderValue) -> Option<&str> {
    match value {
        HeaderValue::Text(text) | HeaderValue::Word(text) => Some(text),
        _ => None,
    }
}

/// A public declaration name: a lowercase SQL identifier that does not begin with `_`.
fn declaration_name(
    header: &[(String, HeaderValue)],
    path: &str,
    kind: &str,
) -> SpecResult<String> {
    let Some(name) = get(header, "name")
        .and_then(text)
        .filter(|name| is_identifier(name))
    else {
        return Err(failure(format!(
            "{path} {kind} name must be a SQL identifier"
        )));
    };
    if name.starts_with('_') {
        return Err(failure(format!(
            "{path} {kind} '{name}' must not begin with '_'; that prefix is reserved for \
             model-private values"
        )));
    }
    if name != name.to_ascii_lowercase() {
        return Err(failure(format!(
            "Invalid public {kind} identity '{name}' in {path}; use snake_case '{}'",
            name.to_ascii_lowercase()
        )));
    }
    Ok(name.to_owned())
}

fn constant(header: &[(String, HeaderValue)], path: &str) -> SpecResult<(String, Constant)> {
    if let Some(unknown) = unknown_keys(header, &CONSTANT_KEYS) {
        return Err(failure(format!(
            "{path} constant has unknown keys: {unknown}"
        )));
    }
    let name: String = declaration_name(header, path, "constant")?;
    let context: String = format!("{path} constant '{name}'");
    let Some(raw) = get(header, "value") else {
        return Err(failure(format!("{context} is missing required value")));
    };
    let option = |key: &str| -> SpecResult<Option<String>> {
        match get(header, key) {
            None => Ok(None),
            Some(value) => match text(value) {
                Some(text) if !text.is_empty() => Ok(Some(text.to_owned())),
                _ => Err(failure(format!("{context} {key} must be an identifier"))),
            },
        }
    };
    let explicit_type: Option<String> = option("type")?;
    let rendering: Option<Rendering> = match option("render_as")?.as_deref() {
        None => None,
        Some("value_list") => Some(Rendering::ValueList),
        Some("array") => Some(Rendering::Array),
        Some(_) => {
            return Err(failure(format!(
                "{context} render_as must be value_list or array"
            )));
        }
    };
    let mut normalization = Normalization {
        context: &context,
        elements: 0,
    };
    let value: ConstantValue = match &explicit_type {
        Some(explicit) => normalization.explicit(raw, explicit)?,
        None => normalization.value(raw, "", 1)?,
    };
    if let Some(rendering) = rendering
        && !matches!(value, ConstantValue::List(_) | ConstantValue::Set(_))
    {
        let mode: &str = match rendering {
            Rendering::ValueList => "value_list",
            Rendering::Array => "array",
        };
        return Err(failure(format!(
            "{context} is {} and does not support render_as {mode}",
            kind_name(&value)
        )));
    }
    Ok((
        name,
        Constant {
            value,
            rendering: rendering.unwrap_or(Rendering::ValueList),
        },
    ))
}

fn kind_name(value: &ConstantValue) -> &'static str {
    match value {
        ConstantValue::Null => "null",
        ConstantValue::Boolean(_) => "boolean",
        ConstantValue::Integer(_) => "integer",
        ConstantValue::Float(_) => "float",
        ConstantValue::Decimal(_) => "decimal",
        ConstantValue::String(_) => "string",
        ConstantValue::List(_) => "list",
        ConstantValue::Set(_) => "set",
        ConstantValue::Object(_) => "object",
    }
}

/// A value's logical type, which collection elements must share: collections include their
/// element type, and objects may hold any values.
fn logical_type(value: &ConstantValue) -> String {
    match value {
        ConstantValue::List(items) => format!("list<{}>", element_type(items)),
        ConstantValue::Set(items) => format!("set<{}>", element_type(items)),
        other => kind_name(other).to_owned(),
    }
}

fn element_type(items: &[ConstantValue]) -> String {
    items
        .iter()
        .find(|item| **item != ConstantValue::Null)
        .map_or_else(|| "null".to_owned(), logical_type)
}

/// Whether `word` looks numeric, so a bare word must be a valid number rather than text.
fn is_number_like(word: &str) -> bool {
    let unsigned: &str = word.strip_prefix(['+', '-']).unwrap_or(word);
    unsigned
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_digit() || first == '.')
}

/// Plain decimal notation: `[+-]digits[.digits]`, `[+-]digits.`, or `[+-].digits`.
fn is_plain_decimal(text: &str) -> bool {
    let unsigned: &str = text.strip_prefix(['+', '-']).unwrap_or(text);
    let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    let digits = |part: &str| part.bytes().all(|byte| byte.is_ascii_digit());
    digits(whole) && digits(fraction) && !(whole.is_empty() && fraction.is_empty())
}

struct Normalization<'a> {
    context: &'a str,
    elements: usize,
}

impl Normalization<'_> {
    fn fail(&self, path: &str, problem: &str) -> SpecError {
        failure(format!("{}{path} {problem}", self.context))
    }

    fn explicit(&mut self, raw: &HeaderValue, explicit: &str) -> SpecResult<ConstantValue> {
        let kind: String = explicit.to_ascii_lowercase();
        if COLLECTION_TYPES.contains(&kind.as_str()) {
            return Err(self.fail(
                "",
                &format!("explicit type '{explicit}' is not a scalar type"),
            ));
        }
        if !SCALAR_TYPES.contains(&kind.as_str()) {
            return Err(self.fail(
                "",
                &format!(
                    "has unsupported explicit type '{explicit}'; expected {}",
                    SCALAR_TYPES.join(", ")
                ),
            ));
        }
        if kind == "decimal" {
            let Some(text) = (match raw {
                HeaderValue::Text(text) => Some(text),
                _ => None,
            }) else {
                return Err(self.fail("", "with type decimal requires a quoted decimal string"));
            };
            let digits: usize = text.bytes().filter(u8::is_ascii_digit).count();
            if !is_plain_decimal(text) {
                return Err(self.fail("", &format!("has invalid decimal value '{text}'")));
            }
            if digits > DECIMAL256_PRECISION {
                return Err(self.fail(
                    "",
                    &format!("decimal has more than {DECIMAL256_PRECISION} digits"),
                ));
            }
            return Ok(ConstantValue::Decimal(text.clone()));
        }
        let value: ConstantValue = self.value(raw, "", 1)?;
        if kind_name(&value) != kind {
            return Err(self.fail(
                "",
                &format!("has type {}; expected {kind}", kind_name(&value)),
            ));
        }
        Ok(value)
    }

    fn value(&mut self, raw: &HeaderValue, path: &str, depth: usize) -> SpecResult<ConstantValue> {
        if depth > MAX_DEPTH {
            return Err(self.fail(
                path,
                &format!("exceeds the maximum nesting depth of {MAX_DEPTH}"),
            ));
        }
        Ok(match raw {
            HeaderValue::Null => ConstantValue::Null,
            HeaderValue::Bool(flag) => ConstantValue::Boolean(*flag),
            HeaderValue::Integer(digits) => {
                ConstantValue::Integer(digits.parse::<i64>().map_err(|_| {
                    self.fail(
                        path,
                        &format!("integer {digits} is outside the signed 64-bit range"),
                    )
                })?)
            }
            HeaderValue::Float(number) if !number.is_finite() => {
                return Err(self.fail(path, "float must be finite"));
            }
            HeaderValue::Float(number) => {
                ConstantValue::Float(if *number == 0.0 { 0.0 } else { *number })
            }
            HeaderValue::Text(text) => ConstantValue::String(text.clone()),
            HeaderValue::Word(word) if is_number_like(word) => {
                return Err(self.fail(
                    path,
                    &format!(
                        "uses bare number-like word '{word}'; write a plain number or quote it"
                    ),
                ));
            }
            HeaderValue::Word(word) => ConstantValue::String(word.clone()),
            HeaderValue::List(items) => {
                ConstantValue::List(self.collection(items, path, depth, "list")?)
            }
            HeaderValue::Set(items) => {
                let values: Vec<ConstantValue> = self.collection(items, path, depth, "set")?;
                ConstantValue::Set(self.canonical_set(values, path)?)
            }
            HeaderValue::Map(entries) => {
                self.count(path, entries.len())?;
                let mut values: Vec<(String, ConstantValue)> = Vec::with_capacity(entries.len());
                for (key, item) in entries {
                    let item_path: String = format!("{path}.{key}");
                    values.push((key.clone(), self.value(item, &item_path, depth + 1)?));
                }
                values.sort_by(|left, right| left.0.cmp(&right.0));
                ConstantValue::Object(values)
            }
        })
    }

    fn count(&mut self, path: &str, count: usize) -> SpecResult<()> {
        self.elements += count;
        if self.elements > MAX_ELEMENTS {
            return Err(self.fail(
                path,
                &format!("exceeds the maximum element count of {MAX_ELEMENTS}"),
            ));
        }
        Ok(())
    }

    fn collection(
        &mut self,
        items: &[HeaderValue],
        path: &str,
        depth: usize,
        kind: &str,
    ) -> SpecResult<Vec<ConstantValue>> {
        if items.is_empty() {
            return Err(self.fail(path, &format!("{kind} must contain at least one value")));
        }
        self.count(path, items.len())?;
        let mut values: Vec<ConstantValue> = Vec::with_capacity(items.len());
        for (index, item) in items.iter().enumerate() {
            values.push(self.value(item, &format!("{path}[{index}]"), depth + 1)?);
        }
        let Some(expected) = values
            .iter()
            .find(|value| **value != ConstantValue::Null)
            .map(logical_type)
        else {
            return Err(self.fail(
                path,
                &format!("{kind} cannot infer an element type from only null values"),
            ));
        };
        for (index, value) in values.iter().enumerate() {
            let actual: String = logical_type(value);
            if *value != ConstantValue::Null && actual != expected {
                return Err(self.fail(
                    &format!("{path}[{index}]"),
                    &format!("has type {actual}; expected {expected}"),
                ));
            }
        }
        Ok(values)
    }

    fn canonical_set(
        &self,
        values: Vec<ConstantValue>,
        path: &str,
    ) -> SpecResult<Vec<ConstantValue>> {
        let mut keyed: Vec<(String, ConstantValue)> = Vec::with_capacity(values.len());
        for (index, value) in values.into_iter().enumerate() {
            let identity: String = format!("{}:{}", logical_type(&value), json(&value));
            if keyed.iter().any(|(seen, _)| *seen == identity) {
                return Err(self.fail(
                    path,
                    &format!(
                        "contains duplicate set value {} at {path}[{index}]",
                        json(&value)
                    ),
                ));
            }
            keyed.push((identity, value));
        }
        keyed.sort_by(|left, right| left.0.cmp(&right.0));
        Ok(keyed.into_iter().map(|(_, value)| value).collect())
    }
}

fn enum_declaration(
    header: &[(String, HeaderValue)],
    path: &str,
) -> SpecResult<(String, Vec<(String, EnumValue)>)> {
    if let Some(unknown) = unknown_keys(header, &ENUM_KEYS) {
        return Err(failure(format!("{path} enum has unknown keys: {unknown}")));
    }
    let name: String = declaration_name(header, path, "enum")?;
    let context: String = format!("{path} enum '{name}'");
    let mut members: Vec<(String, EnumValue)> = Vec::new();
    match get(header, "members") {
        Some(HeaderValue::List(items)) => {
            for item in items {
                let Some(member) = text(item).filter(|member| is_identifier(member)) else {
                    return Err(failure(format!(
                        "{context} shorthand members must be identifiers"
                    )));
                };
                if members.iter().any(|(seen, _)| seen == member) {
                    return Err(failure(format!(
                        "{context} has duplicate member '{member}'"
                    )));
                }
                members.push((member.to_owned(), EnumValue::Text(member.to_owned())));
            }
        }
        Some(HeaderValue::Map(entries)) => {
            for (member, value) in entries {
                if !is_identifier(member) {
                    return Err(failure(format!(
                        "{context} member name must be a SQL identifier"
                    )));
                }
                let value: EnumValue = match value {
                    HeaderValue::Text(text) => EnumValue::Text(text.clone()),
                    HeaderValue::Word(word) if !is_number_like(word) => EnumValue::Text(word.clone()),
                    HeaderValue::Integer(digits) => EnumValue::Integer(digits.parse::<i64>().map_err(|_| {
                        failure(format!(
                            "{context} member '{member}' integer {digits} is outside the signed 64-bit range"
                        ))
                    })?),
                    _ => {
                        return Err(failure(format!(
                            "{context} member '{member}' value must be a string or integer"
                        )));
                    }
                };
                members.push((member.clone(), value));
            }
        }
        _ => {
            return Err(failure(format!(
                "{context} members must use [...] or (...)"
            )));
        }
    }
    if members.is_empty() {
        return Err(failure(format!(
            "{context} must declare at least one member"
        )));
    }
    if let Some((member, _)) = members
        .iter()
        .find(|(member, _)| *member != member.to_ascii_uppercase())
    {
        return Err(failure(format!(
            "{context} member identifiers must be uppercase: '{member}'"
        )));
    }
    let integers: bool = matches!(members[0].1, EnumValue::Integer(_));
    if members
        .iter()
        .any(|(_, value)| matches!(value, EnumValue::Integer(_)) != integers)
    {
        return Err(failure(format!(
            "{context} members must use one consistent scalar type"
        )));
    }
    Ok((name, members))
}

/// JSON text of a value: objects for ClickHouse JSON functions, and set identities.
fn json(value: &ConstantValue) -> String {
    match value {
        ConstantValue::Null => "null".to_owned(),
        ConstantValue::Boolean(flag) => flag.to_string(),
        ConstantValue::Integer(number) => number.to_string(),
        ConstantValue::Float(number) => float_text(*number),
        ConstantValue::Decimal(text) => json_decimal(text),
        ConstantValue::String(text) => json_string(text),
        ConstantValue::List(items) | ConstantValue::Set(items) => {
            format!("[{}]", items.iter().map(json).collect::<Vec<_>>().join(","))
        }
        ConstantValue::Object(entries) => format!(
            "{{{}}}",
            entries
                .iter()
                .map(|(key, item)| format!("{}:{}", json_string(key), json(item)))
                .collect::<Vec<_>>()
                .join(",")
        ),
    }
}

/// A finite float in its shortest round-trip form, always with a fraction or exponent.
fn float_text(number: f64) -> String {
    format!("{number:?}")
}

fn json_decimal(text: &str) -> String {
    let unsigned: &str = text.strip_prefix('+').unwrap_or(text);
    let (sign, digits) = unsigned
        .strip_prefix('-')
        .map_or(("", unsigned), |digits| ("-", digits));
    let (whole, fraction) = digits.split_once('.').unwrap_or((digits, ""));
    let whole: &str = if whole.is_empty() { "0" } else { whole };
    if fraction.is_empty() {
        format!("{sign}{whole}")
    } else {
        format!("{sign}{whole}.{fraction}")
    }
}

fn json_string(text: &str) -> String {
    let mut rendered: String = String::from("\"");
    for character in text.chars() {
        match character {
            '"' => rendered.push_str("\\\""),
            '\\' => rendered.push_str("\\\\"),
            '\n' => rendered.push_str("\\n"),
            '\r' => rendered.push_str("\\r"),
            '\t' => rendered.push_str("\\t"),
            control if u32::from(control) < 0x20 => {
                rendered.push_str(&format!("\\u{:04x}", u32::from(control)));
            }
            other => rendered.push(other),
        }
    }
    rendered.push('"');
    rendered
}

fn string_literal(text: &str) -> SpecResult<String> {
    render_string_literal(text, &SqlDialect::new(CLICKHOUSE_DIALECT))
        .map_err(|error| failure(format!("string literal could not be rendered: {error}")))
}

/// One value as a ClickHouse expression; nested collections are arrays and objects are JSON
/// strings.
fn clickhouse(value: &ConstantValue) -> SpecResult<String> {
    Ok(match value {
        ConstantValue::Null => "NULL".to_owned(),
        ConstantValue::Boolean(flag) => flag.to_string(),
        ConstantValue::Integer(number) => number.to_string(),
        ConstantValue::Float(number) => float_text(*number),
        ConstantValue::Decimal(text) => {
            let precision: usize = text.bytes().filter(u8::is_ascii_digit).count();
            let scale: usize = text
                .split_once('.')
                .map_or(0, |(_, fraction)| fraction.len());
            let function: &str = if precision > DECIMAL128_PRECISION {
                "toDecimal256"
            } else {
                "toDecimal128"
            };
            format!(
                "{function}({}, {scale})",
                string_literal(&json_decimal(text))?
            )
        }
        ConstantValue::String(text) => string_literal(text)?,
        ConstantValue::List(items) | ConstantValue::Set(items) => format!(
            "[{}]",
            items
                .iter()
                .map(clickhouse)
                .collect::<SpecResult<Vec<_>>>()?
                .join(", ")
        ),
        ConstantValue::Object(_) => string_literal(&json(value))?,
    })
}

impl Declarations {
    /// The rendered ClickHouse SQL of constant `name`.
    ///
    /// # Errors
    ///
    /// Returns an error naming the available constants when `name` is unknown.
    pub fn render_constant(&self, name: &str, file_path: &str) -> SpecResult<String> {
        let constant: &Constant = self.constant(name, file_path)?;
        match (&constant.value, constant.rendering) {
            (ConstantValue::List(items) | ConstantValue::Set(items), Rendering::ValueList) => {
                Ok(format!(
                    "({})",
                    items
                        .iter()
                        .map(clickhouse)
                        .collect::<SpecResult<Vec<_>>>()?
                        .join(", ")
                ))
            }
            (value, _) => clickhouse(value),
        }
    }

    /// Constant `name`.
    ///
    /// # Errors
    ///
    /// Returns an error naming the available constants when `name` is unknown.
    pub fn constant(&self, name: &str, file_path: &str) -> SpecResult<&Constant> {
        self.constants.get(name).ok_or_else(|| {
            failure(format!(
                "Unknown constant '{name}' in '{file_path}'. Available constants: {}",
                available(self.constants.keys())
            ))
        })
    }

    /// The value of member `member` of enum `name`.
    ///
    /// # Errors
    ///
    /// Returns an error naming the available enums or members when either is unknown.
    pub fn enum_member(&self, name: &str, member: &str, file_path: &str) -> SpecResult<&EnumValue> {
        let members = self.enums.get(name).ok_or_else(|| {
            failure(format!(
                "Unknown enum '{name}' in '{file_path}'. Available enums: {}",
                available(self.enums.keys())
            ))
        })?;
        members
            .iter()
            .find(|(candidate, _)| candidate == member)
            .map(|(_, value)| value)
            .ok_or_else(|| {
                failure(format!(
                    "Enum '{name}' in '{file_path}' has no member '{member}'. Members: {}",
                    available(members.iter().map(|(candidate, _)| candidate))
                ))
            })
    }

    /// The rendered ClickHouse SQL of member `member` of enum `name`.
    ///
    /// # Errors
    ///
    /// Returns an error naming the available enums or members when either is unknown.
    pub fn render_enum_member(
        &self,
        name: &str,
        member: &str,
        file_path: &str,
    ) -> SpecResult<String> {
        match self.enum_member(name, member, file_path)? {
            EnumValue::Text(text) => string_literal(text),
            EnumValue::Integer(number) => Ok(number.to_string()),
        }
    }
}

fn available<'a>(names: impl Iterator<Item = &'a String>) -> String {
    let names: Vec<&str> = names.map(String::as_str).collect();
    if names.is_empty() {
        "none".to_owned()
    } else {
        names.join(", ")
    }
}
