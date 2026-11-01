//! Extension header blocks: the `MODEL(...);` header of a model file, the `TEST(...);` blocks
//! of a SQL test file, and the name and mode each test header declares.

use std::sync::LazyLock;

use regex::Regex;

use crate::text::{is_python_space, python_strip};
use crate::value::{Kind, Value};

const NAME_KEY: &str = "name";
const MODE_KEY: &str = "mode";
const MODES: [&str; 2] = ["model", "macro"];
const DEFAULT_MODE: &str = "model";

/// Python's `\s` also matches `\x1C`-`\x1F`.
static BLOCK_START: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?ms)^[\s\x1C-\x1F]*TEST[\s\x1C-\x1F]*\((?P<header>.*?)\)[\s\x1C-\x1F]*;[\s\x1C-\x1F]*",
    )
    .expect("valid block start pattern")
});
static BLOCK: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?s)^[\s\x1C-\x1F]*TEST[\s\x1C-\x1F]*\((?P<header>.*?)\)[\s\x1C-\x1F]*;[\s\x1C-\x1F]*(?P<sql>.*)$",
    )
    .expect("valid block pattern")
});

static MODEL_BLOCK: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?s)^[\s\x1C-\x1F]*MODEL[\s\x1C-\x1F]*\((?P<header>.*?)\)[\s\x1C-\x1F]*;[\s\x1C-\x1F]*(?P<sql>.*)$",
    )
    .expect("valid model block pattern")
});

/// A model file's header text, its SQL body, and the one-based line and column, in characters,
/// where the body's first non-whitespace character sits; `None` without a leading header.
#[must_use]
pub fn model_header_block(contents: &str) -> Option<(String, String, usize, usize)> {
    let captures = MODEL_BLOCK.captures(contents)?;
    let sql = captures.name("sql").expect("the pattern captures sql");
    let leading: usize =
        sql.as_str().len() - sql.as_str().trim_start_matches(is_python_space).len();
    let before: &str = &contents[..sql.start() + leading];
    let line: usize = before.matches('\n').count() + 1;
    let column: usize = before
        .rsplit_once('\n')
        .map_or(before, |(_, rest)| rest)
        .chars()
        .count()
        + 1;
    Some((
        captures["header"].to_owned(),
        sql.as_str().to_owned(),
        line,
        column,
    ))
}

/// One `TEST(...);` block's header text and SQL body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockHeader {
    pub header: String,
    pub sql: String,
    /// The character offset of `sql` in the stripped block.
    pub sql_start: usize,
}

/// One block: the character offset where its header match starts, and its header and body,
/// or `None` when the stripped block does not start with a header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub start: usize,
    pub header: Option<BlockHeader>,
}

/// Content before the first `TEST(...);` header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LeadingContent;

/// The blocks of a SQL test file, each running from one line-leading `TEST(...);` header to
/// the next.
///
/// # Errors
///
/// Returns [`LeadingContent`] when non-whitespace text precedes the first header.
pub fn sql_test_blocks(contents: &str) -> Result<Vec<Block>, LeadingContent> {
    let starts: Vec<usize> = BLOCK_START
        .find_iter(contents)
        .map(|found| found.start())
        .collect();
    let Some(first) = starts.first() else {
        return Ok(Vec::new());
    };
    if !python_strip(&contents[..*first]).is_empty() {
        return Err(LeadingContent);
    }
    let mut counted: (usize, usize) = (0, 0);
    Ok(starts
        .iter()
        .enumerate()
        .map(|(position, start)| {
            let end: usize = starts.get(position + 1).copied().unwrap_or(contents.len());
            let block: &str = python_strip(&contents[*start..end]);
            counted = (
                *start,
                counted.1 + contents[counted.0..*start].chars().count(),
            );
            Block {
                start: counted.1,
                header: BLOCK.captures(block).map(|captures| {
                    let sql = captures.name("sql").expect("the pattern captures sql");
                    BlockHeader {
                        header: captures["header"].to_owned(),
                        sql: sql.as_str().to_owned(),
                        sql_start: block[..sql.start()].chars().count(),
                    }
                }),
            }
        })
        .collect())
}

/// The message for the keys of a parsed `TEST()` header other than `name` and `mode`, in
/// header order.
#[must_use]
pub fn unsupported_test_header_keys(
    file_path: &str,
    header: &[(String, &Value)],
) -> Option<String> {
    let unsupported: Vec<&str> = header
        .iter()
        .map(|(key, _)| key.as_str())
        .filter(|key| *key != NAME_KEY && *key != MODE_KEY && *key != "sql_analysis")
        .collect();
    (!unsupported.is_empty()).then(|| {
        format!(
            "TEST() in '{file_path}' only supports `name` and `mode`; unsupported keys: {}",
            unsupported.join(", ")
        )
    })
}

/// The name and mode of a parsed `TEST()` header with supported keys only.
///
/// # Errors
///
/// Returns the message for an empty or non-string name, then for an unknown mode.
pub fn test_header_fields(
    file_path: &str,
    header: &[(String, &Value)],
) -> Result<(Option<String>, String), String> {
    let get = |key: &str| {
        header
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| *value)
            .filter(|value| !value.is_none())
    };
    if header
        .iter()
        .any(|(key, value)| key == "sql_analysis" && !matches!(value.kind, Kind::Bool(_)))
    {
        return Err(format!(
            "TEST() in '{file_path}' sql_analysis must be a boolean"
        ));
    }
    let name: Option<String> = match get(NAME_KEY) {
        None => None,
        Some(value) => match value.as_str().map(python_strip) {
            Some(text) if !text.is_empty() => Some(text.to_owned()),
            _ => {
                return Err(format!(
                    "TEST() name in '{file_path}' must be a non-empty string"
                ));
            }
        },
    };
    let mode: String = match get(MODE_KEY) {
        None => DEFAULT_MODE.to_owned(),
        Some(Value {
            kind: Kind::Str(mode),
            ..
        }) if MODES.contains(&mode.as_str()) => mode.clone(),
        Some(_) => {
            return Err(format!(
                "TEST() mode in '{file_path}' must be one of: {}",
                MODES.join(", ")
            ));
        }
    };
    Ok((name, mode))
}
