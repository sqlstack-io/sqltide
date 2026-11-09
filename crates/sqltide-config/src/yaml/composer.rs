//! Compose `saphyr-parser` events into the node tree of the one document a stream may hold.
//!
//! Adapted from SQLBuild's `sqlbuild-config` composer. SQLTide's YAML is strict: anchors,
//! aliases and a second document are rejected where their events start, in stream order.

use std::borrow::Cow;

use saphyr_parser::{Event, Parser, ScalarStyle, ScanError, Span, StrInput, Tag};

use crate::errors::{ConfigError, ConfigErrorKind};
use crate::yaml::models::{Node, NodeContent};
use crate::yaml::resolver::{MAP_TAG, NON_SPECIFIC_TAG, SEQ_TAG, STR_TAG, resolve_implicit};

const MAX_NESTING_DEPTH: usize = 256;

struct Composer<'input> {
    parser: Parser<'input, StrInput<'input>>,
    chars: Vec<char>,
    previous_end: usize,
    current_end: usize,
}

fn syntax_error(error: &ScanError) -> ConfigError {
    ConfigError::new(ConfigErrorKind::Syntax, error.info())
        .at(error.marker().line(), error.marker().col() + 1)
}

/// The one-based line and column where `span` starts.
fn start_position(span: Span) -> (usize, usize) {
    (span.start.line(), span.start.col() + 1)
}

fn rejected(message: &str, span: Span) -> ConfigError {
    let (line, column) = start_position(span);
    ConfigError::new(ConfigErrorKind::Syntax, message).at(line, column)
}

fn full_tag(tag: Option<&Cow<'_, Tag>>) -> Option<String> {
    tag.map(|tag| format!("{}{}", tag.handle, tag.suffix))
}

/// A scalar's tag: plain scalars and the `!` tag resolve as PyYAML resolves them.
fn scalar_tag(value: &str, style: ScalarStyle, tag: Option<String>) -> String {
    match tag {
        Some(tag) if tag != NON_SPECIFIC_TAG => tag,
        Some(_) => resolve_implicit(value).to_owned(),
        None if style == ScalarStyle::Plain => resolve_implicit(value).to_owned(),
        None => STR_TAG.to_owned(),
    }
}

fn collection_tag(tag: Option<String>, default: &str) -> String {
    match tag {
        Some(tag) if tag != NON_SPECIFIC_TAG => tag,
        _ => default.to_owned(),
    }
}

/// The characters before the first whitespace.
fn first_word(characters: &[char]) -> String {
    characters
        .iter()
        .take_while(|character| !character.is_whitespace())
        .collect()
}

impl<'input> Composer<'input> {
    fn next(&mut self) -> Result<(Event<'input>, Span), ConfigError> {
        match self.parser.next_event() {
            Some(Ok(event)) => {
                self.previous_end = self.current_end;
                self.current_end = event.1.end.index();
                Ok(event)
            }
            Some(Err(error)) => Err(syntax_error(&error)),
            None => Err(ConfigError::new(
                ConfigErrorKind::Syntax,
                "unexpected end of stream",
            )),
        }
    }

    fn char_at(&self, index: usize) -> Option<char> {
        self.chars.get(index).copied()
    }

    /// The `|` or `>` indicator between the previous event and the block scalar's content.
    fn block_indicator(&self, span: Span) -> Option<usize> {
        let mut index: usize = self.previous_end;
        let mut token_start: bool = true;
        while index <= span.start.index() {
            let character: char = self.char_at(index)?;
            match character {
                '|' | '>' => return Some(index),
                '#' if token_start => {
                    while !matches!(self.char_at(index), None | Some('\n' | '\r')) {
                        index += 1;
                    }
                    continue;
                }
                '!' | '&' if token_start => {
                    while !matches!(self.char_at(index), None | Some(' ' | '\n' | '\r')) {
                        index += 1;
                    }
                    continue;
                }
                _ => {}
            }
            token_start = matches!(character, ' ' | '\n' | '\r');
            index += 1;
        }
        None
    }

    /// Where a node with a tag or anchor starts, as PyYAML marks it: its first property token
    /// between the previous event and the node's content.
    fn properties_start(&self, span: Span) -> Option<(usize, usize)> {
        let mut index: usize = self.previous_end;
        let mut token_start: bool = true;
        while index < span.start.index() {
            let character: char = self.char_at(index)?;
            match character {
                '!' | '&' if token_start => return Some(self.position_of(index)),
                '#' if token_start => {
                    while !matches!(self.char_at(index), None | Some('\n' | '\r')) {
                        index += 1;
                    }
                    continue;
                }
                _ => {}
            }
            token_start = matches!(character, ' ' | '\t' | '\n' | '\r' | '[' | '{' | ',');
            index += 1;
        }
        None
    }

    /// The one-based line and column of the character at `index`.
    fn position_of(&self, index: usize) -> (usize, usize) {
        let (mut line, mut column): (usize, usize) = (1, 1);
        for (offset, character) in self.chars[..index].iter().enumerate() {
            let breaks: bool = *character == '\n'
                || (*character == '\r' && self.char_at(offset + 1) != Some('\n'));
            if breaks {
                line += 1;
                column = 1;
            } else {
                column += 1;
            }
        }
        (line, column)
    }

    /// A clipped block scalar that runs to the end of input keeps no line break the input lacks.
    fn without_added_final_break(&self, value: String, style: ScalarStyle, span: Span) -> String {
        let block: bool = matches!(style, ScalarStyle::Literal | ScalarStyle::Folded);
        let at_end: bool = span.end.index() >= self.chars.len();
        let input_ends_with_break: bool = matches!(self.chars.last(), Some('\n' | '\r'));
        let header: String = self
            .block_indicator(span)
            .map(|indicator| first_word(&self.chars[indicator + 1..]))
            .unwrap_or_default();
        let clipped: bool = !header.contains(['+', '-']);
        if block && at_end && clipped && !input_ends_with_break && value.ends_with('\n') {
            value[..value.len() - 1].to_owned()
        } else {
            value
        }
    }

    fn compose_node(
        &mut self,
        event: Event<'input>,
        span: Span,
        depth: usize,
    ) -> Result<Node, ConfigError> {
        let has_properties: bool = match &event {
            Event::Scalar(_, _, anchor, tag)
            | Event::SequenceStart(anchor, tag)
            | Event::MappingStart(anchor, tag) => *anchor != 0 || tag.is_some(),
            _ => false,
        };
        let position: (usize, usize) = has_properties
            .then(|| self.properties_start(span))
            .flatten()
            .unwrap_or_else(|| start_position(span));
        if depth > MAX_NESTING_DEPTH {
            return Err(rejected(
                "nesting deeper than 256 levels is not allowed",
                span,
            ));
        }
        let anchored: bool = match &event {
            Event::Scalar(_, _, anchor, _)
            | Event::SequenceStart(anchor, _)
            | Event::MappingStart(anchor, _) => *anchor != 0,
            _ => false,
        };
        if anchored {
            return Err(
                ConfigError::new(ConfigErrorKind::Syntax, "YAML anchors are not allowed")
                    .at(position.0, position.1),
            );
        }
        match event {
            Event::Alias(_) => Err(rejected("YAML aliases are not allowed", span)),
            Event::Scalar(value, style, _, tag) => {
                let value: String = self.without_added_final_break(value.into_owned(), style, span);
                Ok(Node {
                    tag: scalar_tag(&value, style, full_tag(tag.as_ref())),
                    content: NodeContent::Scalar(value),
                    position,
                })
            }
            Event::SequenceStart(_, tag) => {
                let mut items: Vec<Node> = Vec::new();
                loop {
                    let (event, item_span) = self.next()?;
                    if matches!(event, Event::SequenceEnd) {
                        break;
                    }
                    items.push(self.compose_node(event, item_span, depth + 1)?);
                }
                Ok(Node {
                    tag: collection_tag(full_tag(tag.as_ref()), SEQ_TAG),
                    content: NodeContent::Sequence(items),
                    position,
                })
            }
            Event::MappingStart(_, tag) => {
                let mut entries: Vec<(Node, Node)> = Vec::new();
                loop {
                    let (event, key_span) = self.next()?;
                    if matches!(event, Event::MappingEnd) {
                        break;
                    }
                    let key: Node = self.compose_node(event, key_span, depth + 1)?;
                    let (value_event, value_span) = self.next()?;
                    let value: Node = self.compose_node(value_event, value_span, depth + 1)?;
                    entries.push((key, value));
                }
                Ok(Node {
                    tag: collection_tag(full_tag(tag.as_ref()), MAP_TAG),
                    content: NodeContent::Mapping(entries),
                    position,
                })
            }
            _ => Err(rejected("expected a node", span)),
        }
    }

    fn compose_stream(mut self) -> Result<Option<Node>, ConfigError> {
        self.next()?;
        let (event, _) = self.next()?;
        if matches!(event, Event::StreamEnd) {
            return Ok(None);
        }
        let (root_event, root_span) = self.next()?;
        let root: Node = self.compose_node(root_event, root_span, 0)?;
        self.next()?;
        let (after, after_span) = self.next()?;
        if !matches!(after, Event::StreamEnd) {
            return Err(rejected(
                "multiple YAML documents are not allowed",
                after_span,
            ));
        }
        Ok(Some(root))
    }
}

/// The root node of the only document of `text`; `None` for an empty stream.
pub(crate) fn compose(text: &str) -> Result<Option<Node>, ConfigError> {
    Composer {
        parser: Parser::new_from_str(text),
        chars: text.chars().collect(),
        previous_end: 0,
        current_end: 0,
    }
    .compose_stream()
}
