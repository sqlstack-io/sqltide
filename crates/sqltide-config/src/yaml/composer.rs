//! PyYAML's `Composer` and `Resolver`: events to a node tree with resolved tags.
//!
//! Ported from PyYAML 6.0.3 `yaml/composer.py` and `yaml/resolver.py` (MIT, Copyright (c)
//! 2017-2021 Ingy döt Net, Copyright (c) 2006-2016 Kirill Simonov).

use std::sync::LazyLock;

use regex::Regex;

use crate::yaml::errors::YamlError;
use crate::yaml::parser::{Event, EventKind};
use crate::yaml::reader::Mark;

pub(crate) const STR_TAG: &str = "tag:yaml.org,2002:str";
pub(crate) const SEQ_TAG: &str = "tag:yaml.org,2002:seq";
pub(crate) const MAP_TAG: &str = "tag:yaml.org,2002:map";
pub(crate) const MERGE_TAG: &str = "tag:yaml.org,2002:merge";
pub(crate) const VALUE_TAG: &str = "tag:yaml.org,2002:value";

#[derive(Clone, Debug)]
pub(crate) enum NodeValue {
    Scalar(String),
    Sequence(Vec<Node>),
    Mapping(Vec<(Node, Node)>),
}

#[derive(Clone, Debug)]
pub(crate) struct Node {
    pub(crate) tag: String,
    pub(crate) value: NodeValue,
    pub(crate) start: Mark,
}

impl Node {
    /// The node's `id`, which constructor errors quote.
    pub(crate) fn id(&self) -> &'static str {
        match self.value {
            NodeValue::Scalar(_) => "scalar",
            NodeValue::Sequence(_) => "sequence",
            NodeValue::Mapping(_) => "mapping",
        }
    }
}

/// A Python `re.match` pattern ending in `$`, which also matches before one final newline.
fn python_match(pattern: &str) -> Regex {
    Regex::new(&format!(r"\A(?:{pattern})\n?\z")).expect("the resolver pattern is valid")
}

/// The implicit resolvers in registration order, each with the first characters it applies to.
static IMPLICIT_RESOLVERS: LazyLock<Vec<(&'static str, Regex, &'static str)>> = LazyLock::new(
    || {
        vec![
            (
                "tag:yaml.org,2002:bool",
                python_match(
                    "yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF",
                ),
                "yYnNtTfFoO",
            ),
            (
                "tag:yaml.org,2002:float",
                python_match(
                    r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)",
                ),
                "-+0123456789.",
            ),
            (
                "tag:yaml.org,2002:int",
                python_match(
                    r"[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+",
                ),
                "-+0123456789",
            ),
            (MERGE_TAG, python_match("<<"), "<"),
            (
                "tag:yaml.org,2002:null",
                python_match("~|null|Null|NULL|"),
                "~nN",
            ),
            (
                "tag:yaml.org,2002:timestamp",
                python_match(
                    r"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?",
                ),
                "0123456789",
            ),
            (VALUE_TAG, python_match("="), "="),
            ("tag:yaml.org,2002:yaml", python_match(r"!|&|\*"), "!&*"),
        ]
    },
);

/// `Resolver.resolve` for a scalar: the first implicit resolver for its first character, else
/// `str`. Only the `null` resolver applies to the empty scalar.
fn resolve_scalar(value: &str, implicit: (bool, bool)) -> &'static str {
    if implicit.0 {
        let first: Option<char> = value.chars().next();
        for (tag, pattern, first_characters) in IMPLICIT_RESOLVERS.iter() {
            let applies: bool = match first {
                None => *tag == "tag:yaml.org,2002:null",
                Some(character) => first_characters.contains(character),
            };
            if applies && pattern.is_match(value) {
                return tag;
            }
        }
    }
    STR_TAG
}

/// The node tree of the events of one document, from its node event to its end.
pub(crate) struct Composer<I: Iterator<Item = Event>> {
    events: std::iter::Peekable<I>,
}

impl<I: Iterator<Item = Event>> Composer<I> {
    pub(crate) fn new(events: I) -> Self {
        Self {
            events: events.peekable(),
        }
    }

    /// `get_single_node` over a stream that holds at most one document.
    pub(crate) fn single_node(mut self) -> Result<Option<Node>, YamlError> {
        self.next_event(); // stream start
        let Some(event) = self.next_event() else {
            return Ok(None);
        };
        if !matches!(event.kind, EventKind::DocumentStart) {
            return Ok(None);
        }
        let node: Node = self.compose_node()?;
        self.next_event(); // document end
        Ok(Some(node))
    }

    fn next_event(&mut self) -> Option<Event> {
        self.events.next()
    }

    fn compose_node(&mut self) -> Result<Node, YamlError> {
        let event: Event = self.next_event().expect("a parsed document holds a node");
        let start: Mark = event.start;
        match event.kind {
            EventKind::Scalar {
                tag,
                implicit,
                value,
            } => {
                let tag: String = match tag {
                    Some(tag) if tag != "!" => tag,
                    _ => resolve_scalar(&value, implicit).to_owned(),
                };
                Ok(Node {
                    tag,
                    value: NodeValue::Scalar(value),
                    start,
                })
            }
            EventKind::SequenceStart { tag } => {
                let mut items: Vec<Node> = Vec::new();
                while !matches!(
                    self.events.peek().map(|event| &event.kind),
                    Some(EventKind::SequenceEnd)
                ) {
                    items.push(self.compose_node()?);
                }
                self.next_event();
                Ok(Node {
                    tag: specific_tag(tag, SEQ_TAG),
                    value: NodeValue::Sequence(items),
                    start,
                })
            }
            EventKind::MappingStart { tag } => {
                let mut pairs: Vec<(Node, Node)> = Vec::new();
                while !matches!(
                    self.events.peek().map(|event| &event.kind),
                    Some(EventKind::MappingEnd)
                ) {
                    let key: Node = self.compose_node()?;
                    let value: Node = self.compose_node()?;
                    pairs.push((key, value));
                }
                self.next_event();
                Ok(Node {
                    tag: specific_tag(tag, MAP_TAG),
                    value: NodeValue::Mapping(pairs),
                    start,
                })
            }
            EventKind::Alias => Err(YamlError::internal("YAML aliases are not supported", start)),
            other => unreachable!("the parser yields a node event here, not {other:?}"),
        }
    }
}

fn specific_tag(tag: Option<String>, default: &str) -> String {
    match tag {
        Some(tag) if tag != "!" => tag,
        _ => default.to_owned(),
    }
}
