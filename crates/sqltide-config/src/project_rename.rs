//! In-place edits of a committed project config for `sqt rename-project`: a new `name`, and a
//! `consumer_group_identity` when the rename has to keep existing Kafka consumer groups.
//!
//! Edits splice the parsed value spans into the original text, so every other byte (comments,
//! spacing, key order, line endings) stays exactly as authored.

use std::fmt::Write as _;
use std::ops::Range;

use toml_edit::{Document, Item, Table};

pub const PROJECT_NAME_KEY: &str = "name";
pub const CONSUMER_GROUP_IDENTITY_KEY: &str = "consumer_group_identity";
const IDENTITY_COMMENT: &str =
    "# Keeps the Kafka consumer groups this project used before it was renamed.";

/// The top-level `name` and `consumer_group_identity` a project config declares.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ProjectIdentityKeys {
    pub name: Option<String>,
    pub consumer_group_identity: Option<String>,
}

fn parse(text: &str) -> Result<Document<&str>, String> {
    Document::parse(text).map_err(|error| format!("project config is not valid TOML: {error}"))
}

fn string_at(root: &Table, key: &str) -> Result<Option<(String, Range<usize>)>, String> {
    let Some(item) = root.get(key) else {
        return Ok(None);
    };
    let value = match item {
        Item::Value(value) => value,
        _ => return Err(format!("project config key '{key}' must be a string")),
    };
    let Some(text) = value.as_str() else {
        return Err(format!("project config key '{key}' must be a string"));
    };
    let span: Range<usize> = value
        .span()
        .ok_or_else(|| format!("project config key '{key}' has no source position"))?;
    Ok(Some((text.to_owned(), span)))
}

/// The top-level identity keys of a project config text.
///
/// # Errors
/// A message when the text is not TOML or either key is not a string.
pub fn project_identity_keys(text: &str) -> Result<ProjectIdentityKeys, String> {
    let document: Document<&str> = parse(text)?;
    let root: &Table = document.as_table();
    Ok(ProjectIdentityKeys {
        name: string_at(root, PROJECT_NAME_KEY)?.map(|(value, _)| value),
        consumer_group_identity: string_at(root, CONSUMER_GROUP_IDENTITY_KEY)?
            .map(|(value, _)| value),
    })
}

/// `value` as a TOML basic string.
#[must_use]
pub fn toml_basic_string(value: &str) -> String {
    let mut quoted: String = String::with_capacity(value.len() + 2);
    quoted.push('"');
    for character in value.chars() {
        match character {
            '"' => quoted.push_str("\\\""),
            '\\' => quoted.push_str("\\\\"),
            '\n' => quoted.push_str("\\n"),
            '\t' => quoted.push_str("\\t"),
            '\r' => quoted.push_str("\\r"),
            character if character.is_control() => {
                let _ = write!(quoted, "\\u{:04X}", u32::from(character));
            }
            character => quoted.push(character),
        }
    }
    quoted.push('"');
    quoted
}

/// The project config text with `name` set to `new_name` and, when given, a
/// `consumer_group_identity` line added after the `name` line. Everything else is unchanged.
///
/// # Errors
/// A message when the text is not TOML, has no string `name`, already declares a
/// `consumer_group_identity` while one is to be added, or does not read back as intended.
pub fn rename_project_text(
    text: &str,
    new_name: &str,
    consumer_group_identity: Option<&str>,
) -> Result<String, String> {
    let document: Document<&str> = parse(text)?;
    let root: &Table = document.as_table();
    let (_, name_span) = string_at(root, PROJECT_NAME_KEY)?
        .ok_or_else(|| "project config does not declare a string 'name'".to_owned())?;
    if consumer_group_identity.is_some() && root.contains_key(CONSUMER_GROUP_IDENTITY_KEY) {
        return Err(format!(
            "project config already declares '{CONSUMER_GROUP_IDENTITY_KEY}'"
        ));
    }
    let mut edited: String = String::with_capacity(text.len() + 128);
    edited.push_str(&text[..name_span.start]);
    edited.push_str(&toml_basic_string(new_name));
    let rest: &str = &text[name_span.end..];
    match consumer_group_identity {
        None => edited.push_str(rest),
        Some(identity) => {
            let (line_rest, after) = match rest.find('\n') {
                Some(index) => rest.split_at(index + 1),
                None => (rest, ""),
            };
            let newline: &str = if line_rest.ends_with("\r\n") {
                "\r\n"
            } else {
                "\n"
            };
            edited.push_str(line_rest);
            if !line_rest.ends_with('\n') {
                edited.push_str(newline);
            }
            edited.push_str(IDENTITY_COMMENT);
            edited.push_str(newline);
            edited.push_str(CONSUMER_GROUP_IDENTITY_KEY);
            edited.push_str(" = ");
            edited.push_str(&toml_basic_string(identity));
            edited.push_str(newline);
            edited.push_str(after);
        }
    }
    let read_back: ProjectIdentityKeys = project_identity_keys(&edited)?;
    let expected_identity: Option<String> = match consumer_group_identity {
        Some(identity) => Some(identity.to_owned()),
        None => string_at(root, CONSUMER_GROUP_IDENTITY_KEY)?.map(|(value, _)| value),
    };
    if read_back.name.as_deref() != Some(new_name)
        || read_back.consumer_group_identity != expected_identity
    {
        return Err("project config edit did not read back as intended".to_owned());
    }
    Ok(edited)
}
