//! Canonical SQL data types: Polyglot's generated type with ClickHouse type-name casing.
//!
//! Type names are recased to ClickHouse's vocabulary (`int32` becomes `Int32`) except where a
//! token is not a type: the function name inside `AggregateFunction(...)` and
//! `SimpleAggregateFunction(...)`, and a field name before its type inside `Tuple(...)` or
//! `Nested(...)`. Quoted text is copied unchanged.

use polyglot_sql::{Dialect, DialectType};

use crate::tree::{AnalysisError, AnalysisResult};

const AGGREGATE_STATE_TYPES: [&str; 2] = ["aggregatefunction", "simpleaggregatefunction"];
const NAMED_FIELD_TYPES: [&str; 2] = ["tuple", "nested"];

/// ClickHouse's spelling of a type name, matched case-insensitively.
fn canonical_type_name(token: &str) -> Option<&'static str> {
    Some(match token.to_lowercase().as_str() {
        "int8" => "Int8",
        "int16" => "Int16",
        "int32" => "Int32",
        "int64" => "Int64",
        "int128" => "Int128",
        "int256" => "Int256",
        "uint8" => "UInt8",
        "uint16" => "UInt16",
        "uint32" => "UInt32",
        "uint64" => "UInt64",
        "uint128" => "UInt128",
        "uint256" => "UInt256",
        "float32" => "Float32",
        "float64" => "Float64",
        "aggregatefunction" => "AggregateFunction",
        "array" => "Array",
        "bool" => "Bool",
        "date" => "Date",
        "date32" => "Date32",
        "datetime" => "DateTime",
        "datetime64" => "DateTime64",
        "decimal" => "Decimal",
        "decimal32" => "Decimal32",
        "decimal64" => "Decimal64",
        "decimal128" => "Decimal128",
        "decimal256" => "Decimal256",
        "dynamic" => "Dynamic",
        "enum8" => "Enum8",
        "enum16" => "Enum16",
        "fixedstring" => "FixedString",
        "ipv4" => "IPv4",
        "ipv6" => "IPv6",
        "json" => "JSON",
        "lowcardinality" => "LowCardinality",
        "map" => "Map",
        "nested" => "Nested",
        "nothing" => "Nothing",
        "nullable" => "Nullable",
        "object" => "Object",
        "simpleaggregatefunction" => "SimpleAggregateFunction",
        "string" => "String",
        "tuple" => "Tuple",
        "uuid" => "UUID",
        "variant" => "Variant",
        _ => return None,
    })
}

/// One canonical data type.
///
/// # Errors
///
/// Returns the parse or generation failure, or an unknown dialect.
pub fn normalize_data_type(sql: &str, dialect: &str) -> AnalysisResult<String> {
    let failure = |error: String| {
        AnalysisError(format!(
            "SQL data type could not be parsed with Polyglot: {error}"
        ))
    };
    let dialect_type: DialectType = Dialect::get_by_name(dialect)
        .ok_or_else(|| failure(format!("Unknown dialect: {dialect}")))?
        .dialect_type();
    let data_type = polyglot_sql::parse_data_type(sql, dialect_type)
        .map_err(|error| failure(error.to_string()))?;
    let generated: String = polyglot_sql::generate_data_type(&data_type, dialect_type)
        .map_err(|error| failure(error.to_string()))?;
    Ok(canonicalize_type_tokens(&generated))
}

/// The enclosing parenthesis: the type name that opened it, which argument this is, and
/// whether nothing has been written in that argument yet.
#[derive(Clone)]
struct Context {
    type_name: Option<String>,
    argument: usize,
    at_start: bool,
}

fn is_word(character: char) -> bool {
    character.is_alphanumeric() || character == '_'
}

fn canonicalize_type_tokens(sql: &str) -> String {
    let chars: Vec<char> = sql.chars().collect();
    let mut result: String = String::with_capacity(sql.len());
    let mut contexts: Vec<Context> = Vec::new();
    let mut pending_type_name: Option<String> = None;
    let mut index: usize = 0;
    while index < chars.len() {
        let character: char = chars[index];
        if matches!(character, '\'' | '"' | '`') {
            let end: usize = quoted_end(&chars, index);
            result.extend(&chars[index..end]);
            if let Some(context) = contexts.last_mut() {
                context.at_start = false;
            }
            pending_type_name = None;
            index = end;
            continue;
        }
        if character.is_alphabetic() || character == '_' {
            let mut end: usize = index + 1;
            while end < chars.len() && is_word(chars[end]) {
                end += 1;
            }
            let token: String = chars[index..end].iter().collect();
            let sensitive: bool = is_sensitive_detail(&chars, end, contexts.last());
            let canonical: String = if sensitive {
                token
            } else {
                canonical_type_name(&token).map_or(token, str::to_owned)
            };
            result.push_str(&canonical);
            if let Some(context) = contexts.last_mut() {
                context.at_start = false;
            }
            pending_type_name = (!sensitive).then_some(canonical);
            index = end;
            continue;
        }
        match character {
            '(' => contexts.push(Context {
                type_name: pending_type_name.take(),
                argument: 0,
                at_start: true,
            }),
            ',' if !contexts.is_empty() => {
                if let Some(context) = contexts.last_mut() {
                    context.argument += 1;
                    context.at_start = true;
                }
                pending_type_name = None;
            }
            ')' if !contexts.is_empty() => {
                contexts.pop();
                pending_type_name = None;
            }
            _ => {}
        }
        result.push(character);
        index += 1;
    }
    result
}

fn is_sensitive_detail(chars: &[char], token_end: usize, context: Option<&Context>) -> bool {
    let Some(context) = context else {
        return false;
    };
    let name: String = context
        .type_name
        .as_deref()
        .unwrap_or_default()
        .to_lowercase();
    if AGGREGATE_STATE_TYPES.contains(&name.as_str()) {
        return context.argument == 0;
    }
    if !NAMED_FIELD_TYPES.contains(&name.as_str()) || !context.at_start {
        return false;
    }
    let mut next: usize = token_end;
    while next < chars.len() && chars[next].is_whitespace() {
        next += 1;
    }
    if next == token_end || next >= chars.len() {
        return false;
    }
    let mut next_end: usize = next;
    while next_end < chars.len() && is_word(chars[next_end]) {
        next_end += 1;
    }
    let following: String = chars[next..next_end].iter().collect();
    canonical_type_name(&following).is_some()
}

fn quoted_end(chars: &[char], start: usize) -> usize {
    let quote: char = chars[start];
    let mut index: usize = start + 1;
    while index < chars.len() {
        if chars[index] == '\\' && index + 1 < chars.len() {
            index += 2;
            continue;
        }
        if chars[index] == quote {
            if index + 1 < chars.len() && chars[index + 1] == quote {
                index += 2;
                continue;
            }
            return index + 1;
        }
        index += 1;
    }
    chars.len()
}

#[cfg(test)]
mod tests {
    use super::canonicalize_type_tokens;

    #[test]
    fn recases_types_but_not_field_or_function_names() {
        assert_eq!(
            canonicalize_type_tokens("tuple(string string, x int32)"),
            "Tuple(string String, x Int32)"
        );
        assert_eq!(
            canonicalize_type_tokens("aggregatefunction(uniq, uint64)"),
            "AggregateFunction(uniq, UInt64)"
        );
        assert_eq!(
            canonicalize_type_tokens("enum8('int8' = 1)"),
            "Enum8('int8' = 1)"
        );
    }
}
