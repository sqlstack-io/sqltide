//! A decoded TOML or YAML document, with each node's original Python object kept by handle.

use std::collections::HashMap;

use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::interpolation;

/// One decoded value; `handle` names the original object, which pass-through fields return.
#[derive(Clone, Debug, PartialEq)]
pub struct Value {
    pub handle: usize,
    pub kind: Kind,
    /// Python `str(value)` of a list, tuple, or mapping, which messages and settings embed.
    pub text: Option<String>,
}

/// The shape of one value, distinguished as the Python checks distinguish it.
#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    None,
    Bool(bool),
    /// An `int`, its value when it fits in `i128`, and its `str()`.
    Int(Option<i128>, String),
    /// A `float` and its `str()`.
    Float(f64, String),
    Str(String),
    List(Vec<Value>),
    Tuple(Vec<Value>),
    /// A `dict`, entries in insertion order.
    Dict(Vec<(Value, Value)>),
    /// A `Mapping` that is not a `dict`.
    Mapping(Vec<(Value, Value)>),
    /// Anything else, with its `str()`.
    Other(String),
}

impl Value {
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match &self.kind {
            Kind::Str(text) => Some(text),
            _ => None,
        }
    }

    #[must_use]
    pub const fn is_none(&self) -> bool {
        matches!(self.kind, Kind::None)
    }

    /// The entries of a `dict` or other `Mapping`.
    #[must_use]
    pub fn mapping_entries(&self) -> Option<&[(Self, Self)]> {
        match &self.kind {
            Kind::Dict(entries) | Kind::Mapping(entries) => Some(entries),
            _ => None,
        }
    }

    /// The value under string key `key` of a mapping.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Self> {
        self.mapping_entries()?
            .iter()
            .find(|(entry_key, _)| entry_key.as_str() == Some(key))
            .map(|(_, value)| value)
    }

    /// Python `str(value)` for keys and messages.
    #[must_use]
    pub fn python_str(&self) -> String {
        match &self.kind {
            Kind::None => "None".to_owned(),
            Kind::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
            Kind::Int(_, text) | Kind::Float(_, text) | Kind::Str(text) | Kind::Other(text) => {
                text.clone()
            }
            Kind::List(_) | Kind::Tuple(_) | Kind::Dict(_) | Kind::Mapping(_) => {
                self.text.clone().unwrap_or_default()
            }
        }
    }

    /// Whether any string in the value, including mapping keys, contains `${`.
    #[must_use]
    pub fn contains_interpolation(&self) -> bool {
        match &self.kind {
            Kind::Str(text) => text.contains(crate::INTERPOLATION_TOKEN_START),
            Kind::Dict(entries) | Kind::Mapping(entries) => entries
                .iter()
                .any(|(key, item)| key.contains_interpolation() || item.contains_interpolation()),
            Kind::List(items) | Kind::Tuple(items) => {
                items.iter().any(Self::contains_interpolation)
            }
            _ => false,
        }
    }
}

impl Value {
    /// The interpolation value for this one; mapping keys must be strings.
    ///
    /// # Errors
    ///
    /// Returns a type error for a mapping with a key that is not a string.
    pub fn to_interpolation(&self) -> SpecResult<interpolation::Value> {
        Ok(match &self.kind {
            Kind::Str(text) => interpolation::Value::Str(text.clone()),
            Kind::List(items) | Kind::Tuple(items) => interpolation::Value::Seq(
                items
                    .iter()
                    .map(Self::to_interpolation)
                    .collect::<SpecResult<_>>()?,
            ),
            Kind::Dict(entries) | Kind::Mapping(entries) => interpolation::Value::Map(
                entries
                    .iter()
                    .map(|(key, item)| {
                        let key: &str = key.as_str().ok_or_else(|| {
                            SpecError::new(
                                ErrorKind::Type,
                                "configuration mapping keys must be strings",
                            )
                        })?;
                        Ok((key.to_owned(), item.to_interpolation()?))
                    })
                    .collect::<SpecResult<_>>()?,
            ),
            Kind::None => self.other("null"),
            Kind::Bool(flag) => self.other(if *flag { "true" } else { "false" }),
            _ => self.other(&self.python_str()),
        })
    }

    fn other(&self, text: &str) -> interpolation::Value {
        interpolation::Value::Other {
            handle: self.handle,
            text: text.to_owned(),
        }
    }
}

/// Every value in `value`'s tree, by handle.
pub fn index<'v>(value: &'v Value, objects: &mut HashMap<usize, &'v Value>) {
    objects.insert(value.handle, value);
    match &value.kind {
        Kind::List(items) | Kind::Tuple(items) => {
            items.iter().for_each(|item| index(item, objects));
        }
        Kind::Dict(entries) | Kind::Mapping(entries) => {
            for (key, item) in entries {
                index(key, objects);
                index(item, objects);
            }
        }
        _ => {}
    }
}

/// The decoded value an interpolation result denotes: sequences become tuples, mappings
/// dicts, and untouched values the originals in `objects`.
#[must_use]
pub fn from_interpolation(
    objects: &HashMap<usize, &Value>,
    interpolated: interpolation::Value,
) -> Value {
    let created = |kind: Kind| Value {
        handle: usize::MAX,
        kind,
        text: None,
    };
    match interpolated {
        interpolation::Value::Str(text) => created(Kind::Str(text)),
        interpolation::Value::Seq(items) => created(Kind::Tuple(
            items
                .into_iter()
                .map(|item| from_interpolation(objects, item))
                .collect(),
        )),
        interpolation::Value::Map(entries) => created(Kind::Dict(
            entries
                .into_iter()
                .map(|(key, item)| (created(Kind::Str(key)), from_interpolation(objects, item)))
                .collect(),
        )),
        interpolation::Value::Other { handle, .. } => objects[&handle].clone(),
    }
}
