//! PyYAML's `Parser`: tokens to events, with the same errors at the same marks.
//!
//! Ported from PyYAML 6.0.3 `yaml/parser.py` (MIT, Copyright (c) 2017-2021 Ingy döt Net,
//! Copyright (c) 2006-2016 Kirill Simonov).

use std::collections::HashMap;

use sqltide_sqltext::python_text::python_repr;

use crate::yaml::errors::YamlError;
use crate::yaml::reader::Mark;
use crate::yaml::scanner::{DirectiveValue, Scanner, Token, TokenKind};

const DEFAULT_TAGS: [(&str, &str); 2] = [("!", "!"), ("!!", "tag:yaml.org,2002:")];

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum EventKind {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    Alias,
    /// `implicit` is PyYAML's pair: resolve as plain, resolve as quoted.
    Scalar {
        tag: Option<String>,
        implicit: (bool, bool),
        value: String,
    },
    SequenceStart {
        tag: Option<String>,
    },
    SequenceEnd,
    MappingStart {
        tag: Option<String>,
    },
    MappingEnd,
}

#[derive(Clone, Debug)]
pub(crate) struct Event {
    pub(crate) kind: EventKind,
    /// The anchor of a node event, or the name an alias refers to.
    pub(crate) anchor: Option<String>,
    pub(crate) start: Mark,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    StreamStart,
    ImplicitDocumentStart,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    BlockSequenceFirstEntry,
    BlockSequenceEntry,
    IndentlessSequenceEntry,
    BlockMappingFirstKey,
    BlockMappingKey,
    BlockMappingValue,
    FlowSequenceFirstEntry,
    FlowSequenceEntry,
    FlowSequenceEntryMappingKey,
    FlowSequenceEntryMappingValue,
    FlowSequenceEntryMappingEnd,
    FlowMappingFirstKey,
    FlowMappingKey,
    FlowMappingValue,
    FlowMappingEmptyValue,
}

pub(crate) struct Parser {
    scanner: Scanner,
    tag_handles: HashMap<String, String>,
    states: Vec<State>,
    marks: Vec<Mark>,
    state: Option<State>,
}

type Parse<T> = Result<T, YamlError>;

fn parser_error(problem: String, mark: Mark) -> YamlError {
    YamlError::marked(problem, mark)
}

fn event(kind: EventKind, start: Mark) -> Event {
    Event {
        kind,
        anchor: None,
        start,
    }
}

fn empty_scalar(mark: Mark) -> Event {
    event(
        EventKind::Scalar {
            tag: None,
            implicit: (true, false),
            value: String::new(),
        },
        mark,
    )
}

fn default_tags() -> HashMap<String, String> {
    DEFAULT_TAGS
        .iter()
        .map(|(handle, prefix)| ((*handle).to_owned(), (*prefix).to_owned()))
        .collect()
}

impl Parser {
    pub(crate) fn new(scanner: Scanner) -> Self {
        Self {
            scanner,
            tag_handles: HashMap::new(),
            states: Vec::new(),
            marks: Vec::new(),
            state: Some(State::StreamStart),
        }
    }

    /// The next event, or `None` after the stream end.
    pub(crate) fn next_event(&mut self) -> Parse<Option<Event>> {
        let Some(state) = self.state else {
            return Ok(None);
        };
        self.run(state).map(Some)
    }

    fn is(&mut self, kinds: &[fn(&TokenKind) -> bool]) -> Parse<bool> {
        let kind: &TokenKind = self.scanner.peek_kind()?;
        Ok(kinds.iter().any(|matches| matches(kind)))
    }

    fn peek_start(&mut self) -> Parse<Mark> {
        Ok(self.scanner.peek_token()?.start)
    }

    fn pop_state(&mut self) {
        self.state = self.states.pop();
    }

    fn run(&mut self, state: State) -> Parse<Event> {
        match state {
            State::StreamStart => {
                let token: Token = self.scanner.get_token()?;
                self.state = Some(State::ImplicitDocumentStart);
                Ok(event(EventKind::StreamStart, token.start))
            }
            State::ImplicitDocumentStart => self.parse_implicit_document_start(),
            State::DocumentStart => self.parse_document_start(),
            State::DocumentEnd => self.parse_document_end(),
            State::DocumentContent => self.parse_document_content(),
            State::BlockNode => self.parse_node(true, false),
            State::BlockSequenceFirstEntry => {
                let token: Token = self.scanner.get_token()?;
                self.marks.push(token.start);
                self.parse_block_sequence_entry()
            }
            State::BlockSequenceEntry => self.parse_block_sequence_entry(),
            State::IndentlessSequenceEntry => self.parse_indentless_sequence_entry(),
            State::BlockMappingFirstKey => {
                let token: Token = self.scanner.get_token()?;
                self.marks.push(token.start);
                self.parse_block_mapping_key()
            }
            State::BlockMappingKey => self.parse_block_mapping_key(),
            State::BlockMappingValue => self.parse_block_mapping_value(),
            State::FlowSequenceFirstEntry => {
                let token: Token = self.scanner.get_token()?;
                self.marks.push(token.start);
                self.parse_flow_sequence_entry(true)
            }
            State::FlowSequenceEntry => self.parse_flow_sequence_entry(false),
            State::FlowSequenceEntryMappingKey => self.parse_flow_sequence_entry_mapping_key(),
            State::FlowSequenceEntryMappingValue => self.parse_flow_sequence_entry_mapping_value(),
            State::FlowSequenceEntryMappingEnd => {
                self.state = Some(State::FlowSequenceEntry);
                let start: Mark = self.peek_start()?;
                Ok(event(EventKind::MappingEnd, start))
            }
            State::FlowMappingFirstKey => {
                let token: Token = self.scanner.get_token()?;
                self.marks.push(token.start);
                self.parse_flow_mapping_key(true)
            }
            State::FlowMappingKey => self.parse_flow_mapping_key(false),
            State::FlowMappingValue => self.parse_flow_mapping_value(),
            State::FlowMappingEmptyValue => {
                self.state = Some(State::FlowMappingKey);
                let start: Mark = self.peek_start()?;
                Ok(empty_scalar(start))
            }
        }
    }

    fn parse_implicit_document_start(&mut self) -> Parse<Event> {
        if self.is(&[
            |kind| matches!(kind, TokenKind::Directive(_)),
            |kind| matches!(kind, TokenKind::DocumentStart),
            |kind| matches!(kind, TokenKind::StreamEnd),
        ])? {
            return self.parse_document_start();
        }
        self.tag_handles = default_tags();
        let start: Mark = self.peek_start()?;
        self.states.push(State::DocumentEnd);
        self.state = Some(State::BlockNode);
        Ok(event(EventKind::DocumentStart, start))
    }

    fn parse_document_start(&mut self) -> Parse<Event> {
        while matches!(self.scanner.peek_kind()?, TokenKind::DocumentEnd) {
            self.scanner.get_token()?;
        }
        if matches!(self.scanner.peek_kind()?, TokenKind::StreamEnd) {
            let token: Token = self.scanner.get_token()?;
            self.state = None;
            return Ok(event(EventKind::StreamEnd, token.start));
        }
        let start: Mark = self.peek_start()?;
        self.process_directives()?;
        if !matches!(self.scanner.peek_kind()?, TokenKind::DocumentStart) {
            let token: &Token = self.scanner.peek_token()?;
            return Err(parser_error(
                format!(
                    "expected '<document start>', but found {}",
                    python_repr(token.kind.id())
                ),
                token.start,
            ));
        }
        self.scanner.get_token()?;
        self.states.push(State::DocumentEnd);
        self.state = Some(State::DocumentContent);
        Ok(event(EventKind::DocumentStart, start))
    }

    fn parse_document_end(&mut self) -> Parse<Event> {
        let start: Mark = self.peek_start()?;
        if matches!(self.scanner.peek_kind()?, TokenKind::DocumentEnd) {
            self.scanner.get_token()?;
        }
        self.state = Some(State::DocumentStart);
        Ok(event(EventKind::DocumentEnd, start))
    }

    fn parse_document_content(&mut self) -> Parse<Event> {
        if matches!(
            self.scanner.peek_kind()?,
            TokenKind::Directive(_)
                | TokenKind::DocumentStart
                | TokenKind::DocumentEnd
                | TokenKind::StreamEnd
        ) {
            let start: Mark = self.peek_start()?;
            self.pop_state();
            return Ok(empty_scalar(start));
        }
        self.parse_node(true, false)
    }

    fn process_directives(&mut self) -> Parse<()> {
        let mut yaml_version_seen: bool = false;
        self.tag_handles = HashMap::new();
        while matches!(self.scanner.peek_kind()?, TokenKind::Directive(_)) {
            let token: Token = self.scanner.get_token()?;
            match token.kind {
                TokenKind::Directive(DirectiveValue::Yaml { major }) => {
                    if yaml_version_seen {
                        return Err(parser_error(
                            "found duplicate YAML directive".to_owned(),
                            token.start,
                        ));
                    }
                    if major.trim_start_matches('0') != "1" {
                        return Err(parser_error(
                            "found incompatible YAML document (version 1.* is required)".to_owned(),
                            token.start,
                        ));
                    }
                    yaml_version_seen = true;
                }
                TokenKind::Directive(DirectiveValue::Tag { handle, prefix }) => {
                    if self.tag_handles.contains_key(&handle) {
                        return Err(parser_error(
                            format!("duplicate tag handle {}", python_repr(&handle)),
                            token.start,
                        ));
                    }
                    self.tag_handles.insert(handle, prefix);
                }
                _ => {}
            }
        }
        for (handle, prefix) in DEFAULT_TAGS {
            self.tag_handles
                .entry(handle.to_owned())
                .or_insert_with(|| prefix.to_owned());
        }
        Ok(())
    }

    fn parse_node(&mut self, block: bool, indentless_sequence: bool) -> Parse<Event> {
        if let TokenKind::Alias(_) = self.scanner.peek_kind()? {
            let token: Token = self.scanner.get_token()?;
            let TokenKind::Alias(name) = token.kind else {
                unreachable!("the peeked token is an alias")
            };
            self.pop_state();
            return Ok(Event {
                kind: EventKind::Alias,
                anchor: Some(name),
                start: token.start,
            });
        }
        let mut anchor: Option<String> = None;
        let mut tag: Option<(Option<String>, String)> = None;
        let mut start: Option<Mark> = None;
        let mut tag_mark: Option<Mark> = None;
        if let TokenKind::Anchor(_) = self.scanner.peek_kind()? {
            let token: Token = self.scanner.get_token()?;
            start = Some(token.start);
            if let TokenKind::Anchor(name) = token.kind {
                anchor = Some(name);
            }
            if let TokenKind::Tag(..) = self.scanner.peek_kind()? {
                let token: Token = self.scanner.get_token()?;
                tag_mark = Some(token.start);
                if let TokenKind::Tag(handle, suffix) = token.kind {
                    tag = Some((handle, suffix));
                }
            }
        } else if let TokenKind::Tag(..) = self.scanner.peek_kind()? {
            let token: Token = self.scanner.get_token()?;
            start = Some(token.start);
            tag_mark = Some(token.start);
            if let TokenKind::Tag(handle, suffix) = token.kind {
                tag = Some((handle, suffix));
            }
            if let TokenKind::Anchor(_) = self.scanner.peek_kind()? {
                let token: Token = self.scanner.get_token()?;
                if let TokenKind::Anchor(name) = token.kind {
                    anchor = Some(name);
                }
            }
        }
        let tag: Option<String> = match tag {
            None => None,
            Some((None, suffix)) => Some(suffix),
            Some((Some(handle), suffix)) => match self.tag_handles.get(&handle) {
                Some(prefix) => Some(format!("{prefix}{suffix}")),
                None => {
                    return Err(parser_error(
                        format!("found undefined tag handle {}", python_repr(&handle)),
                        tag_mark.expect("a tag token was read"),
                    ));
                }
            },
        };
        let start: Mark = match start {
            Some(start) => start,
            None => self.peek_start()?,
        };
        let implicit: bool = tag.as_deref().is_none_or(|tag| tag == "!");
        let node = |kind: EventKind, anchor: Option<String>| Event {
            kind,
            anchor,
            start,
        };
        if indentless_sequence && matches!(self.scanner.peek_kind()?, TokenKind::BlockEntry) {
            self.state = Some(State::IndentlessSequenceEntry);
            return Ok(node(EventKind::SequenceStart { tag }, anchor));
        }
        let next_kind: TokenKind = self.scanner.peek_kind()?.clone();
        match next_kind {
            TokenKind::Scalar { .. } => {
                let token: Token = self.scanner.get_token()?;
                let TokenKind::Scalar { value, plain } = token.kind else {
                    unreachable!("the peeked token is a scalar")
                };
                let implicit: (bool, bool) =
                    if (plain && tag.is_none()) || tag.as_deref() == Some("!") {
                        (true, false)
                    } else if tag.is_none() {
                        (false, true)
                    } else {
                        (false, false)
                    };
                self.pop_state();
                Ok(node(
                    EventKind::Scalar {
                        tag,
                        implicit,
                        value,
                    },
                    anchor,
                ))
            }
            TokenKind::FlowSequenceStart => {
                self.state = Some(State::FlowSequenceFirstEntry);
                Ok(node(EventKind::SequenceStart { tag }, anchor))
            }
            TokenKind::FlowMappingStart => {
                self.state = Some(State::FlowMappingFirstKey);
                Ok(node(EventKind::MappingStart { tag }, anchor))
            }
            TokenKind::BlockSequenceStart if block => {
                self.state = Some(State::BlockSequenceFirstEntry);
                Ok(node(EventKind::SequenceStart { tag }, anchor))
            }
            TokenKind::BlockMappingStart if block => {
                self.state = Some(State::BlockMappingFirstKey);
                Ok(node(EventKind::MappingStart { tag }, anchor))
            }
            _ if anchor.is_some() || tag.is_some() => {
                self.pop_state();
                Ok(node(
                    EventKind::Scalar {
                        tag,
                        implicit: (implicit, false),
                        value: String::new(),
                    },
                    anchor,
                ))
            }
            other => {
                let token_start: Mark = self.peek_start()?;
                let _ = block;
                Err(parser_error(
                    format!(
                        "expected the node content, but found {}",
                        python_repr(other.id())
                    ),
                    token_start,
                ))
            }
        }
    }

    fn unexpected(&mut self, expected: &str) -> Parse<Event> {
        let token: &Token = self.scanner.peek_token()?;
        Err(parser_error(
            format!("{expected}{}", python_repr(token.kind.id())),
            token.start,
        ))
    }

    fn parse_block_sequence_entry(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::BlockEntry) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::BlockEntry | TokenKind::BlockEnd
            ) {
                self.states.push(State::BlockSequenceEntry);
                return self.parse_node(true, false);
            }
            self.state = Some(State::BlockSequenceEntry);
            return Ok(empty_scalar(token.end));
        }
        if !matches!(self.scanner.peek_kind()?, TokenKind::BlockEnd) {
            return self.unexpected("expected <block end>, but found ");
        }
        let token: Token = self.scanner.get_token()?;
        self.pop_state();
        self.marks.pop();
        Ok(event(EventKind::SequenceEnd, token.start))
    }

    fn parse_indentless_sequence_entry(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::BlockEntry) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::BlockEntry | TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
            ) {
                self.states.push(State::IndentlessSequenceEntry);
                return self.parse_node(true, false);
            }
            self.state = Some(State::IndentlessSequenceEntry);
            return Ok(empty_scalar(token.end));
        }
        let start: Mark = self.peek_start()?;
        self.pop_state();
        Ok(event(EventKind::SequenceEnd, start))
    }

    fn parse_block_mapping_key(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::Key) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
            ) {
                self.states.push(State::BlockMappingValue);
                return self.parse_node(true, true);
            }
            self.state = Some(State::BlockMappingValue);
            return Ok(empty_scalar(token.end));
        }
        if !matches!(self.scanner.peek_kind()?, TokenKind::BlockEnd) {
            return self.unexpected("expected <block end>, but found ");
        }
        let token: Token = self.scanner.get_token()?;
        self.pop_state();
        self.marks.pop();
        Ok(event(EventKind::MappingEnd, token.start))
    }

    fn parse_block_mapping_value(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::Value) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
            ) {
                self.states.push(State::BlockMappingKey);
                return self.parse_node(true, true);
            }
            self.state = Some(State::BlockMappingKey);
            return Ok(empty_scalar(token.end));
        }
        self.state = Some(State::BlockMappingKey);
        let start: Mark = self.peek_start()?;
        Ok(empty_scalar(start))
    }

    fn parse_flow_sequence_entry(&mut self, first: bool) -> Parse<Event> {
        if !matches!(self.scanner.peek_kind()?, TokenKind::FlowSequenceEnd) {
            if !first {
                if matches!(self.scanner.peek_kind()?, TokenKind::FlowEntry) {
                    self.scanner.get_token()?;
                } else {
                    return self.unexpected("expected ',' or ']', but got ");
                }
            }
            if matches!(self.scanner.peek_kind()?, TokenKind::Key) {
                let start: Mark = self.peek_start()?;
                self.state = Some(State::FlowSequenceEntryMappingKey);
                return Ok(event(EventKind::MappingStart { tag: None }, start));
            }
            if !matches!(self.scanner.peek_kind()?, TokenKind::FlowSequenceEnd) {
                self.states.push(State::FlowSequenceEntry);
                return self.parse_node(false, false);
            }
        }
        let token: Token = self.scanner.get_token()?;
        self.pop_state();
        self.marks.pop();
        Ok(event(EventKind::SequenceEnd, token.start))
    }

    fn parse_flow_sequence_entry_mapping_key(&mut self) -> Parse<Event> {
        let token: Token = self.scanner.get_token()?;
        if !matches!(
            self.scanner.peek_kind()?,
            TokenKind::Value | TokenKind::FlowEntry | TokenKind::FlowSequenceEnd
        ) {
            self.states.push(State::FlowSequenceEntryMappingValue);
            return self.parse_node(false, false);
        }
        self.state = Some(State::FlowSequenceEntryMappingValue);
        Ok(empty_scalar(token.end))
    }

    fn parse_flow_sequence_entry_mapping_value(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::Value) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::FlowEntry | TokenKind::FlowSequenceEnd
            ) {
                self.states.push(State::FlowSequenceEntryMappingEnd);
                return self.parse_node(false, false);
            }
            self.state = Some(State::FlowSequenceEntryMappingEnd);
            return Ok(empty_scalar(token.end));
        }
        self.state = Some(State::FlowSequenceEntryMappingEnd);
        let start: Mark = self.peek_start()?;
        Ok(empty_scalar(start))
    }

    fn parse_flow_mapping_key(&mut self, first: bool) -> Parse<Event> {
        if !matches!(self.scanner.peek_kind()?, TokenKind::FlowMappingEnd) {
            if !first {
                if matches!(self.scanner.peek_kind()?, TokenKind::FlowEntry) {
                    self.scanner.get_token()?;
                } else {
                    return self.unexpected("expected ',' or '}', but got ");
                }
            }
            if matches!(self.scanner.peek_kind()?, TokenKind::Key) {
                let token: Token = self.scanner.get_token()?;
                if !matches!(
                    self.scanner.peek_kind()?,
                    TokenKind::Value | TokenKind::FlowEntry | TokenKind::FlowMappingEnd
                ) {
                    self.states.push(State::FlowMappingValue);
                    return self.parse_node(false, false);
                }
                self.state = Some(State::FlowMappingValue);
                return Ok(empty_scalar(token.end));
            }
            if !matches!(self.scanner.peek_kind()?, TokenKind::FlowMappingEnd) {
                self.states.push(State::FlowMappingEmptyValue);
                return self.parse_node(false, false);
            }
        }
        let token: Token = self.scanner.get_token()?;
        self.pop_state();
        self.marks.pop();
        Ok(event(EventKind::MappingEnd, token.start))
    }

    fn parse_flow_mapping_value(&mut self) -> Parse<Event> {
        if matches!(self.scanner.peek_kind()?, TokenKind::Value) {
            let token: Token = self.scanner.get_token()?;
            if !matches!(
                self.scanner.peek_kind()?,
                TokenKind::FlowEntry | TokenKind::FlowMappingEnd
            ) {
                self.states.push(State::FlowMappingKey);
                return self.parse_node(false, false);
            }
            self.state = Some(State::FlowMappingKey);
            return Ok(empty_scalar(token.end));
        }
        self.state = Some(State::FlowMappingKey);
        let start: Mark = self.peek_start()?;
        Ok(empty_scalar(start))
    }
}
