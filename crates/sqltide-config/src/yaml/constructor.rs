//! Build values from a composed document with PyYAML's safe tags.
//!
//! Callers only tell strings, nulls, lists and dicts apart, so every other safe type (`bool`,
//! `int`, `float`, `timestamp`, `binary`, `set`) is built as `Other` without decoding its text.

use crate::errors::{ConfigError, ConfigErrorKind};
use crate::yaml::models::{Node, NodeContent};
use crate::yaml::resolver::{
    BOOL_TAG, FLOAT_TAG, INT_TAG, MAP_TAG, NULL_TAG, SEQ_TAG, STR_TAG, TIMESTAMP_TAG, VALUE_TAG,
};

const BINARY_TAG: &str = "tag:yaml.org,2002:binary";
const SET_TAG: &str = "tag:yaml.org,2002:set";
const OMAP_TAG: &str = "tag:yaml.org,2002:omap";
const PAIRS_TAG: &str = "tag:yaml.org,2002:pairs";
const COLLECTION_TAGS: [&str; 5] = [SEQ_TAG, MAP_TAG, SET_TAG, OMAP_TAG, PAIRS_TAG];
const BOOL_WORDS: [&str; 6] = ["yes", "no", "true", "false", "on", "off"];

/// A constructed value, by the Python type `yaml.safe_load` returns for it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    None,
    Str(String),
    /// A `list`.
    List(Vec<Value>),
    /// A `dict` in insertion order: a repeated string key keeps its first position and its last
    /// value.
    Dict(Vec<(Value, Value)>),
    /// Any other type: a number, boolean, date, bytes, set or `!!omap` pair.
    Other,
}

type Construct<T> = Result<T, ConfigError>;

fn error(message: impl Into<String>, node: &Node) -> ConfigError {
    ConfigError::new(ConfigErrorKind::Construct, message).at(node.position.0, node.position.1)
}

/// `repr` of a tag, which holds no characters Python escapes.
fn tag_repr(tag: &str) -> String {
    if tag.contains('\'') && !tag.contains('"') {
        format!("\"{tag}\"")
    } else {
        format!("'{}'", tag.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// `SafeConstructor.construct_scalar`: a mapping stands for the value of its `=` key.
fn scalar(node: &Node) -> Construct<&str> {
    match &node.content {
        NodeContent::Scalar(text) => Ok(text),
        NodeContent::Mapping(pairs) if pairs.iter().any(|(key, _)| key.tag == VALUE_TAG) => {
            let (_, value) = pairs
                .iter()
                .find(|(key, _)| key.tag == VALUE_TAG)
                .expect("a value key was found");
            scalar(value)
        }
        _ => Err(error(
            format!("expected a scalar node, but found {}", node.kind()),
            node,
        )),
    }
}

/// The value of the document whose root is `node`.
pub(crate) fn construct(node: &Node) -> Construct<Value> {
    match node.tag.as_str() {
        NULL_TAG => scalar(node).map(|_| Value::None),
        STR_TAG => scalar(node).map(|text| Value::Str(text.to_owned())),
        BOOL_TAG => {
            let word: String = scalar(node)?.to_lowercase();
            if BOOL_WORDS.contains(&word.as_str()) {
                Ok(Value::Other)
            } else {
                Err(error(format!("{word:?} is not a YAML boolean"), node))
            }
        }
        INT_TAG | FLOAT_TAG | TIMESTAMP_TAG | BINARY_TAG => scalar(node).map(|_| Value::Other),
        SEQ_TAG => match &node.content {
            NodeContent::Sequence(items) => items
                .iter()
                .map(construct)
                .collect::<Construct<_>>()
                .map(Value::List),
            _ => Err(error(
                format!("expected a sequence node, but found {}", node.kind()),
                node,
            )),
        },
        MAP_TAG => construct_mapping(node).map(Value::Dict),
        SET_TAG => construct_mapping(node).map(|_| Value::Other),
        OMAP_TAG | PAIRS_TAG => construct_pairs(node),
        tag => Err(error(
            format!(
                "could not determine a constructor for the tag {}",
                tag_repr(tag)
            ),
            node,
        )),
    }
}

/// A mapping's entries; a `=` key is the string `=`, and a collection-tagged key is unhashable,
/// as PyYAML builds collections empty before filling them.
fn construct_mapping(node: &Node) -> Construct<Vec<(Value, Value)>> {
    let NodeContent::Mapping(pairs) = &node.content else {
        return Err(error(
            format!("expected a mapping node, but found {}", node.kind()),
            node,
        ));
    };
    let mut entries: Vec<(Value, Value)> = Vec::with_capacity(pairs.len());
    for (key_node, value_node) in pairs {
        let key: Value = if key_node.tag == VALUE_TAG {
            Value::Str(scalar(key_node)?.to_owned())
        } else if COLLECTION_TAGS.contains(&key_node.tag.as_str()) {
            return Err(error("found unhashable key", key_node));
        } else {
            construct(key_node)?
        };
        let value: Value = construct(value_node)?;
        match entries
            .iter_mut()
            .find(|(existing, _)| matches!(existing, Value::Str(_)) && *existing == key)
        {
            Some(entry) => entry.1 = value,
            None => entries.push((key, value)),
        }
    }
    Ok(entries)
}

/// `!!omap` and `!!pairs`: a sequence of single-entry mappings, built as a list of pairs.
fn construct_pairs(node: &Node) -> Construct<Value> {
    let NodeContent::Sequence(items) = &node.content else {
        return Err(error(
            format!("expected a sequence, but found {}", node.kind()),
            node,
        ));
    };
    let mut pairs: Vec<Value> = Vec::with_capacity(items.len());
    for item in items {
        let NodeContent::Mapping(entries) = &item.content else {
            return Err(error(
                format!("expected a mapping of length 1, but found {}", item.kind()),
                item,
            ));
        };
        let [(key, value)] = entries.as_slice() else {
            return Err(error(
                format!(
                    "expected a single mapping item, but found {} items",
                    entries.len()
                ),
                item,
            ));
        };
        construct(key)?;
        construct(value)?;
        pairs.push(Value::Other);
    }
    Ok(Value::List(pairs))
}
