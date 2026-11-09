//! PyYAML's YAML 1.1 implicit resolvers for plain scalars.
//!
//! The patterns are PyYAML 6.0.3's `yaml/resolver.py` (MIT, Copyright (c) 2017-2021 Ingy döt
//! Net, Copyright (c) 2006-2016 Kirill Simonov), so `yes`, `on`, `0o17`-style octals and dates
//! keep the types they had under `yaml.safe_load`.

use std::sync::LazyLock;

use regex::Regex;

pub(crate) const NULL_TAG: &str = "tag:yaml.org,2002:null";
pub(crate) const BOOL_TAG: &str = "tag:yaml.org,2002:bool";
pub(crate) const INT_TAG: &str = "tag:yaml.org,2002:int";
pub(crate) const FLOAT_TAG: &str = "tag:yaml.org,2002:float";
pub(crate) const TIMESTAMP_TAG: &str = "tag:yaml.org,2002:timestamp";
pub(crate) const STR_TAG: &str = "tag:yaml.org,2002:str";
pub(crate) const MERGE_TAG: &str = "tag:yaml.org,2002:merge";
pub(crate) const VALUE_TAG: &str = "tag:yaml.org,2002:value";
/// The tag PyYAML resolves `!`, `&` and `*` to, which no safe constructor builds.
const YAML_TAG: &str = "tag:yaml.org,2002:yaml";
pub(crate) const SEQ_TAG: &str = "tag:yaml.org,2002:seq";
pub(crate) const MAP_TAG: &str = "tag:yaml.org,2002:map";
pub(crate) const NON_SPECIFIC_TAG: &str = "!";

/// A Python `re.match` pattern ending in `$`, which also matches before one final newline.
fn python_match(pattern: &str) -> Regex {
    Regex::new(&format!(r"\A(?:{pattern})\n?\z")).expect("the resolver pattern is valid")
}

/// The implicit resolvers in registration order, each with the first characters it applies to.
static IMPLICIT_RESOLVERS: LazyLock<[(&str, Regex, &str); 8]> = LazyLock::new(|| {
    [
        (
            BOOL_TAG,
            python_match(
                "yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF",
            ),
            "yYnNtTfFoO",
        ),
        (
            FLOAT_TAG,
            python_match(
                r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)",
            ),
            "-+0123456789.",
        ),
        (
            INT_TAG,
            python_match(
                r"[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+",
            ),
            "-+0123456789",
        ),
        (MERGE_TAG, python_match("<<"), "<"),
        (NULL_TAG, python_match("~|null|Null|NULL|"), "~nN"),
        (
            TIMESTAMP_TAG,
            python_match(
                r"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?",
            ),
            "0123456789",
        ),
        (VALUE_TAG, python_match("="), "="),
        (YAML_TAG, python_match(r"!|&|\*"), "!&*"),
    ]
});

/// The tag of a plain or `!`-tagged scalar: the first resolver registered for its first
/// character that matches, else `str`. Only the `null` resolver applies to the empty scalar.
pub(crate) fn resolve_implicit(value: &str) -> &'static str {
    let first: Option<char> = value.chars().next();
    IMPLICIT_RESOLVERS
        .iter()
        .find(|(tag, pattern, first_characters)| {
            let applies: bool = match first {
                None => *tag == NULL_TAG,
                Some(character) => first_characters.contains(character),
            };
            applies && pattern.is_match(value)
        })
        .map_or(STR_TAG, |(tag, _, _)| tag)
}
