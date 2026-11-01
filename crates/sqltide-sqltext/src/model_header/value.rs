//! Values a MODEL header produces.

/// One parsed header value. Integers keep their decimal text so callers can build an
/// arbitrary-precision integer, as Python's `int()` does.
#[derive(Clone, Debug, PartialEq)]
pub enum HeaderValue {
    Null,
    Bool(bool),
    Integer(String),
    Float(f64),
    Text(String),
    List(Vec<HeaderValue>),
    Map(Vec<(String, HeaderValue)>),
    /// A bare word that is not a literal; only declaration headers keep it apart from `Text`.
    Word(String),
    /// A `{...}` set; only declaration headers accept braces.
    Set(Vec<HeaderValue>),
}

/// Convert a bare word into a literal, or keep it as text, or as a `Word` in a declaration.
pub(super) fn word_value(word: &str, declaration: bool) -> HeaderValue {
    match word {
        "true" => HeaderValue::Bool(true),
        "false" => HeaderValue::Bool(false),
        "null" => HeaderValue::Null,
        _ if is_integer(word) => HeaderValue::Integer(word.to_owned()),
        _ if is_float(word) => word
            .parse::<f64>()
            .map_or_else(|_| HeaderValue::Text(word.to_owned()), HeaderValue::Float),
        _ if declaration => HeaderValue::Word(word.to_owned()),
        _ => HeaderValue::Text(word.to_owned()),
    }
}

/// `^[+-]?\d+$` with ASCII digits.
fn is_integer(word: &str) -> bool {
    let digits: &str = strip_sign(word);
    !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
}

/// `^[+-]?(?:\d+\.\d*|\d*\.\d+)$` with ASCII digits.
fn is_float(word: &str) -> bool {
    let Some((whole, fraction)) = strip_sign(word).split_once('.') else {
        return false;
    };
    let all_digits = |part: &str| part.bytes().all(|byte| byte.is_ascii_digit());
    all_digits(whole) && all_digits(fraction) && !(whole.is_empty() && fraction.is_empty())
}

fn strip_sign(word: &str) -> &str {
    word.strip_prefix(['+', '-']).unwrap_or(word)
}

/// Insert or replace a key, keeping the first insertion position like a Python `dict`.
pub(super) fn insert_entry(
    entries: &mut Vec<(String, HeaderValue)>,
    key: String,
    value: HeaderValue,
) {
    match entries.iter_mut().find(|(existing, _)| *existing == key) {
        Some(entry) => entry.1 = value,
        None => entries.push((key, value)),
    }
}
