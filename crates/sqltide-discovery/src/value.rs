//! A decoded TOML or YAML document, with each node's original Python object kept by handle.

/// One decoded value; `handle` names the original object, which pass-through fields return.
#[derive(Clone, Debug, PartialEq)]
pub struct Value {
    pub handle: usize,
    pub kind: Kind,
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
            Kind::List(_) | Kind::Tuple(_) | Kind::Dict(_) | Kind::Mapping(_) => String::new(),
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
