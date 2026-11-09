//! PyYAML's `Scanner`: characters to tokens, with the same errors at the same marks.
//!
//! Ported from PyYAML 6.0.3 `yaml/scanner.py` (MIT, Copyright (c) 2017-2021 Ingy döt Net,
//! Copyright (c) 2006-2016 Kirill Simonov).

use std::collections::VecDeque;

use sqltide_sqltext::python_text::python_repr;

use crate::yaml::_helpers::python::{MAX_INT_DIGITS, decode_utf8};
use crate::yaml::errors::YamlError;
use crate::yaml::reader::{
    BREAK_OR_BLANK_OR_END, BREAK_OR_END, BREAK_OR_SPACE_OR_END, LINE_BREAKS, Mark, Reader, is_in,
};

const HEX_DIGITS: &str = "0123456789ABCDEFabcdef";
const URI_CHARACTERS: &str = "-;/?:@&=+$,_.!~*'()[]%";

/// A directive's parsed value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum DirectiveValue {
    /// `%YAML major.minor`, with `major` as its decimal digits.
    Yaml {
        major: String,
    },
    /// `%TAG handle prefix`.
    Tag {
        handle: String,
        prefix: String,
    },
    Other,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum TokenKind {
    Directive(DirectiveValue),
    DocumentStart,
    DocumentEnd,
    StreamStart,
    StreamEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowMappingStart,
    FlowSequenceEnd,
    FlowMappingEnd,
    Key,
    Value,
    BlockEntry,
    FlowEntry,
    Alias(String),
    Anchor(String),
    Tag(Option<String>, String),
    Scalar { value: String, plain: bool },
}

impl TokenKind {
    /// The token's `id`, which parser errors quote.
    pub(crate) fn id(&self) -> &'static str {
        match self {
            Self::Directive(_) => "<directive>",
            Self::DocumentStart => "<document start>",
            Self::DocumentEnd => "<document end>",
            Self::StreamStart => "<stream start>",
            Self::StreamEnd => "<stream end>",
            Self::BlockSequenceStart => "<block sequence start>",
            Self::BlockMappingStart => "<block mapping start>",
            Self::BlockEnd => "<block end>",
            Self::FlowSequenceStart => "[",
            Self::FlowMappingStart => "{",
            Self::FlowSequenceEnd => "]",
            Self::FlowMappingEnd => "}",
            Self::Key => "?",
            Self::Value => ":",
            Self::BlockEntry => "-",
            Self::FlowEntry => ",",
            Self::Alias(_) => "<alias>",
            Self::Anchor(_) => "<anchor>",
            Self::Tag(..) => "<tag>",
            Self::Scalar { .. } => "<scalar>",
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct Token {
    pub(crate) kind: TokenKind,
    pub(crate) start: Mark,
    pub(crate) end: Mark,
}

#[derive(Clone, Copy, Debug)]
struct SimpleKey {
    token_number: usize,
    required: bool,
    index: usize,
    line: usize,
    column: usize,
    mark: Mark,
}

pub(crate) struct Scanner {
    reader: Reader,
    done: bool,
    /// PyYAML lets a stray closing bracket take this below zero.
    flow_level: i64,
    tokens: VecDeque<Token>,
    tokens_taken: usize,
    indent: i64,
    indents: Vec<i64>,
    allow_simple_key: bool,
    /// Keyed by flow level, in insertion order like PyYAML's dict.
    possible_simple_keys: Vec<(i64, SimpleKey)>,
}

type Scan<T> = Result<T, YamlError>;

fn scanner_error(problem: String, mark: Mark) -> YamlError {
    YamlError::marked(problem, mark)
}

fn char_repr(character: char) -> String {
    python_repr(&character.to_string())
}

impl Scanner {
    pub(crate) fn new(reader: Reader) -> Self {
        let mut scanner = Self {
            reader,
            done: false,
            flow_level: 0,
            tokens: VecDeque::new(),
            tokens_taken: 0,
            indent: -1,
            indents: Vec::new(),
            allow_simple_key: true,
            possible_simple_keys: Vec::new(),
        };
        let mark: Mark = scanner.reader.mark();
        scanner.tokens.push_back(Token {
            kind: TokenKind::StreamStart,
            start: mark,
            end: mark,
        });
        scanner
    }

    fn peek(&self, index: usize) -> char {
        self.reader.peek(index)
    }

    fn forward(&mut self, length: usize) {
        self.reader.forward(length);
    }

    fn mark(&self) -> Mark {
        self.reader.mark()
    }

    fn column(&self) -> i64 {
        self.reader.column() as i64
    }

    /// The next token without consuming it.
    pub(crate) fn peek_token(&mut self) -> Scan<&Token> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }
        Ok(self
            .tokens
            .front()
            .expect("the scanner always ends with a stream end token"))
    }

    /// The next token's kind; PyYAML's `check_token` with one choice compares this.
    pub(crate) fn peek_kind(&mut self) -> Scan<&TokenKind> {
        Ok(&self.peek_token()?.kind)
    }

    pub(crate) fn get_token(&mut self) -> Scan<Token> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }
        self.tokens_taken += 1;
        Ok(self
            .tokens
            .pop_front()
            .expect("the scanner always ends with a stream end token"))
    }

    fn need_more_tokens(&mut self) -> Scan<bool> {
        if self.done {
            return Ok(false);
        }
        if self.tokens.is_empty() {
            return Ok(true);
        }
        self.stale_possible_simple_keys()?;
        Ok(self.next_possible_simple_key() == Some(self.tokens_taken))
    }

    fn fetch_more_tokens(&mut self) -> Scan<()> {
        self.scan_to_next_token();
        self.stale_possible_simple_keys()?;
        self.unwind_indent(self.column());
        let character: char = self.peek(0);
        match character {
            '\0' => return self.fetch_stream_end(),
            '%' if self.check_directive() => return self.fetch_directive(),
            '-' if self.check_document_start() => return self.fetch_document_indicator(true),
            '.' if self.check_document_end() => return self.fetch_document_indicator(false),
            '[' => return self.fetch_flow_collection_start(TokenKind::FlowSequenceStart),
            '{' => return self.fetch_flow_collection_start(TokenKind::FlowMappingStart),
            ']' => return self.fetch_flow_collection_end(TokenKind::FlowSequenceEnd),
            '}' => return self.fetch_flow_collection_end(TokenKind::FlowMappingEnd),
            ',' => return self.fetch_flow_entry(),
            '-' if self.check_block_entry() => return self.fetch_block_entry(),
            '?' if self.check_key() => return self.fetch_key(),
            ':' if self.check_value() => return self.fetch_value(),
            '*' => return self.fetch_anchor(true),
            '&' => return self.fetch_anchor(false),
            '!' => return self.fetch_tag(),
            '|' if self.flow_level == 0 => return self.fetch_block_scalar(false),
            '>' if self.flow_level == 0 => return self.fetch_block_scalar(true),
            '\'' => return self.fetch_flow_scalar(false),
            '"' => return self.fetch_flow_scalar(true),
            _ => {}
        }
        if self.check_plain() {
            return self.fetch_plain();
        }
        Err(scanner_error(
            format!(
                "found character {} that cannot start any token",
                char_repr(character)
            ),
            self.mark(),
        ))
    }

    fn next_possible_simple_key(&self) -> Option<usize> {
        self.possible_simple_keys
            .iter()
            .map(|(_, key)| key.token_number)
            .min()
    }

    fn stale_possible_simple_keys(&mut self) -> Scan<()> {
        let line: usize = self.reader.line();
        let index: usize = self.reader.index();
        let mut kept: Vec<(i64, SimpleKey)> = Vec::new();
        for (level, key) in std::mem::take(&mut self.possible_simple_keys) {
            if key.line != line || index - key.index > 1024 {
                if key.required {
                    return Err(scanner_error(
                        "could not find expected ':'".to_owned(),
                        self.mark(),
                    ));
                }
            } else {
                kept.push((level, key));
            }
        }
        self.possible_simple_keys = kept;
        Ok(())
    }

    fn save_possible_simple_key(&mut self) -> Scan<()> {
        let required: bool = self.flow_level == 0 && self.indent == self.column();
        if self.allow_simple_key {
            self.remove_possible_simple_key()?;
            let token_number: usize = self.tokens_taken + self.tokens.len();
            let mark: Mark = self.mark();
            self.possible_simple_keys.push((
                self.flow_level,
                SimpleKey {
                    token_number,
                    required,
                    index: mark.index,
                    line: mark.line,
                    column: mark.column,
                    mark,
                },
            ));
        }
        Ok(())
    }

    fn remove_possible_simple_key(&mut self) -> Scan<()> {
        if let Some(position) = self
            .possible_simple_keys
            .iter()
            .position(|(level, _)| *level == self.flow_level)
        {
            if self.possible_simple_keys[position].1.required {
                return Err(scanner_error(
                    "could not find expected ':'".to_owned(),
                    self.mark(),
                ));
            }
            self.possible_simple_keys.remove(position);
        }
        Ok(())
    }

    fn unwind_indent(&mut self, column: i64) {
        if self.flow_level != 0 {
            return;
        }
        while self.indent > column {
            let mark: Mark = self.mark();
            self.indent = self.indents.pop().unwrap_or(-1);
            self.tokens.push_back(Token {
                kind: TokenKind::BlockEnd,
                start: mark,
                end: mark,
            });
        }
    }

    fn add_indent(&mut self, column: i64) -> bool {
        if self.indent < column {
            self.indents.push(self.indent);
            self.indent = column;
            return true;
        }
        false
    }

    fn push_simple(&mut self, kind: TokenKind, length: usize) {
        let start: Mark = self.mark();
        self.forward(length);
        let end: Mark = self.mark();
        self.tokens.push_back(Token { kind, start, end });
    }

    fn fetch_stream_end(&mut self) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.possible_simple_keys.clear();
        let mark: Mark = self.mark();
        self.tokens.push_back(Token {
            kind: TokenKind::StreamEnd,
            start: mark,
            end: mark,
        });
        self.done = true;
        Ok(())
    }

    fn fetch_directive(&mut self) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let token: Token = self.scan_directive()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_document_indicator(&mut self, start: bool) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let kind: TokenKind = if start {
            TokenKind::DocumentStart
        } else {
            TokenKind::DocumentEnd
        };
        self.push_simple(kind, 3);
        Ok(())
    }

    fn fetch_flow_collection_start(&mut self, kind: TokenKind) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.flow_level += 1;
        self.allow_simple_key = true;
        self.push_simple(kind, 1);
        Ok(())
    }

    fn fetch_flow_collection_end(&mut self, kind: TokenKind) -> Scan<()> {
        self.remove_possible_simple_key()?;
        self.flow_level -= 1;
        self.allow_simple_key = false;
        self.push_simple(kind, 1);
        Ok(())
    }

    fn fetch_flow_entry(&mut self) -> Scan<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_simple(TokenKind::FlowEntry, 1);
        Ok(())
    }

    fn fetch_block_entry(&mut self) -> Scan<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return Err(scanner_error(
                    "sequence entries are not allowed here".to_owned(),
                    self.mark(),
                ));
            }
            if self.add_indent(self.column()) {
                let mark: Mark = self.mark();
                self.tokens.push_back(Token {
                    kind: TokenKind::BlockSequenceStart,
                    start: mark,
                    end: mark,
                });
            }
        }
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_simple(TokenKind::BlockEntry, 1);
        Ok(())
    }

    fn fetch_key(&mut self) -> Scan<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return Err(scanner_error(
                    "mapping keys are not allowed here".to_owned(),
                    self.mark(),
                ));
            }
            if self.add_indent(self.column()) {
                let mark: Mark = self.mark();
                self.tokens.push_back(Token {
                    kind: TokenKind::BlockMappingStart,
                    start: mark,
                    end: mark,
                });
            }
        }
        self.allow_simple_key = self.flow_level == 0;
        self.remove_possible_simple_key()?;
        self.push_simple(TokenKind::Key, 1);
        Ok(())
    }

    fn fetch_value(&mut self) -> Scan<()> {
        if let Some(position) = self
            .possible_simple_keys
            .iter()
            .position(|(level, _)| *level == self.flow_level)
        {
            let (_, key) = self.possible_simple_keys.remove(position);
            let at: usize = key.token_number - self.tokens_taken;
            self.tokens.insert(
                at,
                Token {
                    kind: TokenKind::Key,
                    start: key.mark,
                    end: key.mark,
                },
            );
            if self.flow_level == 0 && self.add_indent(key.column as i64) {
                self.tokens.insert(
                    at,
                    Token {
                        kind: TokenKind::BlockMappingStart,
                        start: key.mark,
                        end: key.mark,
                    },
                );
            }
            self.allow_simple_key = false;
        } else {
            if self.flow_level == 0 && !self.allow_simple_key {
                return Err(scanner_error(
                    "mapping values are not allowed here".to_owned(),
                    self.mark(),
                ));
            }
            if self.flow_level == 0 && self.add_indent(self.column()) {
                let mark: Mark = self.mark();
                self.tokens.push_back(Token {
                    kind: TokenKind::BlockMappingStart,
                    start: mark,
                    end: mark,
                });
            }
            self.allow_simple_key = self.flow_level == 0;
            self.remove_possible_simple_key()?;
        }
        self.push_simple(TokenKind::Value, 1);
        Ok(())
    }

    fn fetch_anchor(&mut self, alias: bool) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token: Token = self.scan_anchor(alias)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_tag(&mut self) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token: Token = self.scan_tag()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_block_scalar(&mut self, folded: bool) -> Scan<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let token: Token = self.scan_block_scalar(folded)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_scalar(&mut self, double: bool) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token: Token = self.scan_flow_scalar(double)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_plain(&mut self) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token: Token = self.scan_plain()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn check_directive(&self) -> bool {
        self.reader.column() == 0
    }

    fn check_document_start(&self) -> bool {
        self.reader.column() == 0
            && self.reader.prefix(3) == "---"
            && is_in(self.peek(3), BREAK_OR_BLANK_OR_END)
    }

    fn check_document_end(&self) -> bool {
        self.reader.column() == 0
            && self.reader.prefix(3) == "..."
            && is_in(self.peek(3), BREAK_OR_BLANK_OR_END)
    }

    fn check_block_entry(&self) -> bool {
        is_in(self.peek(1), BREAK_OR_BLANK_OR_END)
    }

    fn check_key(&self) -> bool {
        self.flow_level != 0 || is_in(self.peek(1), BREAK_OR_BLANK_OR_END)
    }

    fn check_value(&self) -> bool {
        self.flow_level != 0 || is_in(self.peek(1), BREAK_OR_BLANK_OR_END)
    }

    fn check_plain(&self) -> bool {
        let character: char = self.peek(0);
        !is_in(
            character,
            "\0 \t\r\n\u{85}\u{2028}\u{2029}-?:,[]{}#&*!|>'\"%@`",
        ) || (!is_in(self.peek(1), BREAK_OR_BLANK_OR_END)
            && (character == '-' || (self.flow_level == 0 && is_in(character, "?:"))))
    }

    fn scan_to_next_token(&mut self) {
        if self.reader.index() == 0 && self.peek(0) == '\u{feff}' {
            self.forward(1);
        }
        loop {
            while self.peek(0) == ' ' {
                self.forward(1);
            }
            if self.peek(0) == '#' {
                while !is_in(self.peek(0), BREAK_OR_END) {
                    self.forward(1);
                }
            }
            if self.scan_line_break().is_empty() {
                return;
            }
            if self.flow_level == 0 {
                self.allow_simple_key = true;
            }
        }
    }

    fn scan_directive(&mut self) -> Scan<Token> {
        let start: Mark = self.mark();
        self.forward(1);
        let name: String = self.scan_directive_name()?;
        let value: DirectiveValue = match name.as_str() {
            "YAML" => self.scan_yaml_directive_value()?,
            "TAG" => self.scan_tag_directive_value()?,
            _ => DirectiveValue::Other,
        };
        let end: Mark = self.mark();
        if value == DirectiveValue::Other {
            while !is_in(self.peek(0), BREAK_OR_END) {
                self.forward(1);
            }
        }
        self.scan_directive_ignored_line()?;
        Ok(Token {
            kind: TokenKind::Directive(value),
            start,
            end,
        })
    }

    fn word_length(&self, start: usize) -> usize {
        let mut length: usize = start;
        while self.peek(length).is_ascii_alphanumeric() || is_in(self.peek(length), "-_") {
            length += 1;
        }
        length
    }

    fn scan_directive_name(&mut self) -> Scan<String> {
        let length: usize = self.word_length(0);
        if length == 0 {
            return Err(scanner_error(
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    char_repr(self.peek(0))
                ),
                self.mark(),
            ));
        }
        let value: String = self.reader.prefix(length);
        self.forward(length);
        let character: char = self.peek(0);
        if !is_in(character, BREAK_OR_SPACE_OR_END) {
            return Err(scanner_error(
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    char_repr(character)
                ),
                self.mark(),
            ));
        }
        Ok(value)
    }

    fn scan_yaml_directive_value(&mut self) -> Scan<DirectiveValue> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        let major: String = self.scan_yaml_directive_number()?;
        if self.peek(0) != '.' {
            return Err(scanner_error(
                format!(
                    "expected a digit or '.', but found {}",
                    char_repr(self.peek(0))
                ),
                self.mark(),
            ));
        }
        self.forward(1);
        self.scan_yaml_directive_number()?;
        if !is_in(self.peek(0), BREAK_OR_SPACE_OR_END) {
            return Err(scanner_error(
                format!(
                    "expected a digit or ' ', but found {}",
                    char_repr(self.peek(0))
                ),
                self.mark(),
            ));
        }
        Ok(DirectiveValue::Yaml { major })
    }

    fn scan_yaml_directive_number(&mut self) -> Scan<String> {
        let character: char = self.peek(0);
        if !character.is_ascii_digit() {
            return Err(scanner_error(
                format!("expected a digit, but found {}", char_repr(character)),
                self.mark(),
            ));
        }
        let mut length: usize = 0;
        while self.peek(length).is_ascii_digit() {
            length += 1;
        }
        if length > MAX_INT_DIGITS {
            return Err(YamlError::internal(
                format!(
                    "Exceeds the limit ({MAX_INT_DIGITS} digits) for integer string conversion: \
                     value has {length} digits; use sys.set_int_max_str_digits() to increase \
                     the limit"
                ),
                self.mark(),
            ));
        }
        let value: String = self.reader.prefix(length);
        self.forward(length);
        Ok(value)
    }

    fn scan_tag_directive_value(&mut self) -> Scan<DirectiveValue> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        let handle: String = self.scan_tag_handle("directive")?;
        if self.peek(0) != ' ' {
            return Err(scanner_error(
                format!("expected ' ', but found {}", char_repr(self.peek(0))),
                self.mark(),
            ));
        }
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        let prefix: String = self.scan_tag_uri("directive")?;
        if !is_in(self.peek(0), BREAK_OR_SPACE_OR_END) {
            return Err(scanner_error(
                format!("expected ' ', but found {}", char_repr(self.peek(0))),
                self.mark(),
            ));
        }
        Ok(DirectiveValue::Tag { handle, prefix })
    }

    fn scan_directive_ignored_line(&mut self) -> Scan<()> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        if self.peek(0) == '#' {
            while !is_in(self.peek(0), BREAK_OR_END) {
                self.forward(1);
            }
        }
        let character: char = self.peek(0);
        if !is_in(character, BREAK_OR_END) {
            return Err(scanner_error(
                format!(
                    "expected a comment or a line break, but found {}",
                    char_repr(character)
                ),
                self.mark(),
            ));
        }
        self.scan_line_break();
        Ok(())
    }

    fn scan_anchor(&mut self, alias: bool) -> Scan<Token> {
        let start: Mark = self.mark();
        self.forward(1);
        let length: usize = self.word_length(0);
        if length == 0 {
            return Err(scanner_error(
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    char_repr(self.peek(0))
                ),
                self.mark(),
            ));
        }
        let value: String = self.reader.prefix(length);
        self.forward(length);
        let character: char = self.peek(0);
        if !is_in(character, "\0 \t\r\n\u{85}\u{2028}\u{2029}?:,]}%@`") {
            return Err(scanner_error(
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    char_repr(character)
                ),
                self.mark(),
            ));
        }
        let end: Mark = self.mark();
        let kind: TokenKind = if alias {
            TokenKind::Alias(value)
        } else {
            TokenKind::Anchor(value)
        };
        Ok(Token { kind, start, end })
    }

    fn scan_tag(&mut self) -> Scan<Token> {
        let start: Mark = self.mark();
        let mut character: char = self.peek(1);
        let handle: Option<String>;
        let suffix: String;
        if character == '<' {
            handle = None;
            self.forward(2);
            suffix = self.scan_tag_uri("tag")?;
            if self.peek(0) != '>' {
                return Err(scanner_error(
                    format!("expected '>', but found {}", char_repr(self.peek(0))),
                    self.mark(),
                ));
            }
            self.forward(1);
        } else if is_in(character, BREAK_OR_BLANK_OR_END) {
            handle = None;
            suffix = "!".to_owned();
            self.forward(1);
        } else {
            let mut length: usize = 1;
            let mut use_handle: bool = false;
            while !is_in(character, BREAK_OR_SPACE_OR_END) {
                if character == '!' {
                    use_handle = true;
                    break;
                }
                length += 1;
                character = self.peek(length);
            }
            if use_handle {
                handle = Some(self.scan_tag_handle("tag")?);
            } else {
                handle = Some("!".to_owned());
                self.forward(1);
            }
            suffix = self.scan_tag_uri("tag")?;
        }
        if !is_in(self.peek(0), BREAK_OR_SPACE_OR_END) {
            return Err(scanner_error(
                format!("expected ' ', but found {}", char_repr(self.peek(0))),
                self.mark(),
            ));
        }
        let end: Mark = self.mark();
        Ok(Token {
            kind: TokenKind::Tag(handle, suffix),
            start,
            end,
        })
    }

    fn scan_block_scalar(&mut self, folded: bool) -> Scan<Token> {
        let mut chunks: String = String::new();
        let start: Mark = self.mark();
        self.forward(1);
        let (chomping, increment) = self.scan_block_scalar_indicators()?;
        self.scan_block_scalar_ignored_line()?;
        let min_indent: i64 = (self.indent + 1).max(1);
        let indent: i64;
        let mut breaks: Vec<String>;
        let mut end: Mark;
        match increment {
            None => {
                let (found_breaks, max_indent, found_end) = self.scan_block_scalar_indentation();
                breaks = found_breaks;
                end = found_end;
                indent = min_indent.max(max_indent);
            }
            Some(increment) => {
                indent = min_indent + increment - 1;
                (breaks, end) = self.scan_block_scalar_breaks(indent);
            }
        }
        let mut line_break: String = String::new();
        while self.column() == indent && self.peek(0) != '\0' {
            chunks.extend(breaks.iter().map(String::as_str));
            let leading_non_space: bool = !is_in(self.peek(0), " \t");
            let mut length: usize = 0;
            while !is_in(self.peek(length), BREAK_OR_END) {
                length += 1;
            }
            chunks.push_str(&self.reader.prefix(length));
            self.forward(length);
            line_break = self.scan_line_break();
            (breaks, end) = self.scan_block_scalar_breaks(indent);
            if self.column() == indent && self.peek(0) != '\0' {
                if folded && line_break == "\n" && leading_non_space && !is_in(self.peek(0), " \t")
                {
                    if breaks.is_empty() {
                        chunks.push(' ');
                    }
                } else {
                    chunks.push_str(&line_break);
                }
            } else {
                break;
            }
        }
        if chomping != Some(false) {
            chunks.push_str(&line_break);
        }
        if chomping == Some(true) {
            chunks.extend(breaks.iter().map(String::as_str));
        }
        Ok(Token {
            kind: TokenKind::Scalar {
                value: chunks,
                plain: false,
            },
            start,
            end,
        })
    }

    fn indentation_indicator(&mut self) -> Scan<Option<i64>> {
        let character: char = self.peek(0);
        let Some(digit) = character
            .to_digit(10)
            .filter(|_| character.is_ascii_digit())
        else {
            return Ok(None);
        };
        if digit == 0 {
            return Err(scanner_error(
                "expected indentation indicator in the range 1-9, but found 0".to_owned(),
                self.mark(),
            ));
        }
        self.forward(1);
        Ok(Some(i64::from(digit)))
    }

    fn scan_block_scalar_indicators(&mut self) -> Scan<(Option<bool>, Option<i64>)> {
        let mut chomping: Option<bool> = None;
        let mut increment: Option<i64> = None;
        let character: char = self.peek(0);
        if is_in(character, "+-") {
            chomping = Some(character == '+');
            self.forward(1);
            increment = self.indentation_indicator()?;
        } else if character.is_ascii_digit() {
            increment = self.indentation_indicator()?;
            let character: char = self.peek(0);
            if is_in(character, "+-") {
                chomping = Some(character == '+');
                self.forward(1);
            }
        }
        let character: char = self.peek(0);
        if !is_in(character, BREAK_OR_SPACE_OR_END) {
            return Err(scanner_error(
                format!(
                    "expected chomping or indentation indicators, but found {}",
                    char_repr(character)
                ),
                self.mark(),
            ));
        }
        Ok((chomping, increment))
    }

    fn scan_block_scalar_ignored_line(&mut self) -> Scan<()> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        if self.peek(0) == '#' {
            while !is_in(self.peek(0), BREAK_OR_END) {
                self.forward(1);
            }
        }
        let character: char = self.peek(0);
        if !is_in(character, BREAK_OR_END) {
            return Err(scanner_error(
                format!(
                    "expected a comment or a line break, but found {}",
                    char_repr(character)
                ),
                self.mark(),
            ));
        }
        self.scan_line_break();
        Ok(())
    }

    fn scan_block_scalar_indentation(&mut self) -> (Vec<String>, i64, Mark) {
        let mut chunks: Vec<String> = Vec::new();
        let mut max_indent: i64 = 0;
        let mut end: Mark = self.mark();
        while is_in(self.peek(0), " \r\n\u{85}\u{2028}\u{2029}") {
            if self.peek(0) != ' ' {
                chunks.push(self.scan_line_break());
                end = self.mark();
            } else {
                self.forward(1);
                max_indent = max_indent.max(self.column());
            }
        }
        (chunks, max_indent, end)
    }

    fn scan_block_scalar_breaks(&mut self, indent: i64) -> (Vec<String>, Mark) {
        let mut chunks: Vec<String> = Vec::new();
        let mut end: Mark = self.mark();
        while self.column() < indent && self.peek(0) == ' ' {
            self.forward(1);
        }
        while is_in(self.peek(0), LINE_BREAKS) {
            chunks.push(self.scan_line_break());
            end = self.mark();
            while self.column() < indent && self.peek(0) == ' ' {
                self.forward(1);
            }
        }
        (chunks, end)
    }

    fn scan_flow_scalar(&mut self, double: bool) -> Scan<Token> {
        let mut chunks: String = String::new();
        let start: Mark = self.mark();
        let quote: char = self.peek(0);
        self.forward(1);
        self.scan_flow_scalar_non_spaces(double, &mut chunks)?;
        while self.peek(0) != quote {
            self.scan_flow_scalar_spaces(double, &mut chunks)?;
            self.scan_flow_scalar_non_spaces(double, &mut chunks)?;
        }
        self.forward(1);
        let end: Mark = self.mark();
        Ok(Token {
            kind: TokenKind::Scalar {
                value: chunks,
                plain: false,
            },
            start,
            end,
        })
    }

    fn scan_flow_scalar_non_spaces(&mut self, double: bool, chunks: &mut String) -> Scan<()> {
        loop {
            let mut length: usize = 0;
            while !is_in(self.peek(length), "'\"\\\0 \t\r\n\u{85}\u{2028}\u{2029}") {
                length += 1;
            }
            if length > 0 {
                chunks.push_str(&self.reader.prefix(length));
                self.forward(length);
            }
            let character: char = self.peek(0);
            if !double && character == '\'' && self.peek(1) == '\'' {
                chunks.push('\'');
                self.forward(2);
            } else if (double && character == '\'') || (!double && is_in(character, "\"\\")) {
                chunks.push(character);
                self.forward(1);
            } else if double && character == '\\' {
                self.forward(1);
                let character: char = self.peek(0);
                if let Some(replacement) = escape_replacement(character) {
                    chunks.push(replacement);
                    self.forward(1);
                } else if let Some(length) = escape_code_length(character) {
                    self.forward(1);
                    for offset in 0..length {
                        if !is_in(self.peek(offset), HEX_DIGITS) {
                            return Err(scanner_error(
                                format!(
                                    "expected escape sequence of {length} hexadecimal numbers, \
                                     but found {}",
                                    char_repr(self.peek(offset))
                                ),
                                self.mark(),
                            ));
                        }
                    }
                    let code: u32 = u32::from_str_radix(&self.reader.prefix(length), 16)
                        .expect("the escape holds only hexadecimal digits");
                    let Some(escaped) = char::from_u32(code) else {
                        let problem: String = if code > 0x10_ffff {
                            "chr() arg not in range(0x110000)".to_owned()
                        } else {
                            format!(
                                "found an escape for the lone surrogate U+{code:04X}, \
                                 which SQLTide does not accept"
                            )
                        };
                        return Err(YamlError::internal(problem, self.mark()));
                    };
                    chunks.push(escaped);
                    self.forward(length);
                } else if is_in(character, LINE_BREAKS) {
                    self.scan_line_break();
                    self.scan_flow_scalar_breaks(chunks)?;
                } else {
                    return Err(scanner_error(
                        format!("found unknown escape character {}", char_repr(character)),
                        self.mark(),
                    ));
                }
            } else {
                return Ok(());
            }
        }
    }

    fn scan_flow_scalar_spaces(&mut self, double: bool, chunks: &mut String) -> Scan<()> {
        let _ = double;
        let mut length: usize = 0;
        while is_in(self.peek(length), " \t") {
            length += 1;
        }
        let whitespaces: String = self.reader.prefix(length);
        self.forward(length);
        let character: char = self.peek(0);
        if character == '\0' {
            return Err(scanner_error(
                "found unexpected end of stream".to_owned(),
                self.mark(),
            ));
        }
        if is_in(character, LINE_BREAKS) {
            let line_break: String = self.scan_line_break();
            let mut breaks: String = String::new();
            let found: usize = self.scan_flow_scalar_breaks(&mut breaks)?;
            if line_break != "\n" {
                chunks.push_str(&line_break);
            } else if found == 0 {
                chunks.push(' ');
            }
            chunks.push_str(&breaks);
        } else {
            chunks.push_str(&whitespaces);
        }
        Ok(())
    }

    /// Appends the line breaks to `chunks` and returns how many there were.
    fn scan_flow_scalar_breaks(&mut self, chunks: &mut String) -> Scan<usize> {
        let mut found: usize = 0;
        loop {
            let prefix: String = self.reader.prefix(3);
            if (prefix == "---" || prefix == "...") && is_in(self.peek(3), BREAK_OR_BLANK_OR_END) {
                return Err(scanner_error(
                    "found unexpected document separator".to_owned(),
                    self.mark(),
                ));
            }
            while is_in(self.peek(0), " \t") {
                self.forward(1);
            }
            if is_in(self.peek(0), LINE_BREAKS) {
                chunks.push_str(&self.scan_line_break());
                found += 1;
            } else {
                return Ok(found);
            }
        }
    }

    fn scan_plain(&mut self) -> Scan<Token> {
        let mut chunks: String = String::new();
        let start: Mark = self.mark();
        let mut end: Mark = start;
        let indent: i64 = self.indent + 1;
        let mut spaces: Option<String> = Some(String::new());
        loop {
            let mut length: usize = 0;
            if self.peek(0) == '#' {
                break;
            }
            loop {
                let character: char = self.peek(length);
                let after: char = self.peek(length + 1);
                let ends_value: bool = character == ':'
                    && (is_in(after, BREAK_OR_BLANK_OR_END)
                        || (self.flow_level != 0 && is_in(after, ",[]{}")));
                if is_in(character, BREAK_OR_BLANK_OR_END)
                    || ends_value
                    || (self.flow_level != 0 && is_in(character, ",?[]{}"))
                {
                    break;
                }
                length += 1;
            }
            if length == 0 {
                break;
            }
            self.allow_simple_key = false;
            chunks.push_str(spaces.as_deref().unwrap_or_default());
            chunks.push_str(&self.reader.prefix(length));
            self.forward(length);
            end = self.mark();
            spaces = self.scan_plain_spaces();
            let Some(found) = spaces.as_deref() else {
                break;
            };
            if found.is_empty()
                || self.peek(0) == '#'
                || (self.flow_level == 0 && self.column() < indent)
            {
                break;
            }
        }
        Ok(Token {
            kind: TokenKind::Scalar {
                value: chunks,
                plain: true,
            },
            start,
            end,
        })
    }

    /// The text joining two plain scalar lines; `None` where PyYAML returns no chunk list.
    fn scan_plain_spaces(&mut self) -> Option<String> {
        let mut chunks: String = String::new();
        let mut length: usize = 0;
        while self.peek(length) == ' ' {
            length += 1;
        }
        let whitespaces: String = self.reader.prefix(length);
        self.forward(length);
        let character: char = self.peek(0);
        if is_in(character, LINE_BREAKS) {
            let line_break: String = self.scan_line_break();
            self.allow_simple_key = true;
            if self.at_document_separator() {
                return None;
            }
            let mut breaks: String = String::new();
            let mut found: usize = 0;
            while is_in(self.peek(0), " \r\n\u{85}\u{2028}\u{2029}") {
                if self.peek(0) == ' ' {
                    self.forward(1);
                } else {
                    breaks.push_str(&self.scan_line_break());
                    found += 1;
                    if self.at_document_separator() {
                        return None;
                    }
                }
            }
            if line_break != "\n" {
                chunks.push_str(&line_break);
            } else if found == 0 {
                chunks.push(' ');
            }
            chunks.push_str(&breaks);
        } else if !whitespaces.is_empty() {
            chunks.push_str(&whitespaces);
        }
        Some(chunks)
    }

    fn at_document_separator(&self) -> bool {
        let prefix: String = self.reader.prefix(3);
        (prefix == "---" || prefix == "...") && is_in(self.peek(3), BREAK_OR_BLANK_OR_END)
    }

    fn scan_tag_handle(&mut self, name: &str) -> Scan<String> {
        let _ = name;
        let character: char = self.peek(0);
        if character != '!' {
            return Err(scanner_error(
                format!("expected '!', but found {}", char_repr(character)),
                self.mark(),
            ));
        }
        let mut length: usize = 1;
        if self.peek(length) != ' ' {
            length = self.word_length(length);
            let character: char = self.peek(length);
            if character != '!' {
                self.forward(length);
                return Err(scanner_error(
                    format!("expected '!', but found {}", char_repr(character)),
                    self.mark(),
                ));
            }
            length += 1;
        }
        let value: String = self.reader.prefix(length);
        self.forward(length);
        Ok(value)
    }

    fn scan_tag_uri(&mut self, name: &str) -> Scan<String> {
        let mut chunks: String = String::new();
        let mut any_chunk: bool = false;
        let mut length: usize = 0;
        let mut character: char = self.peek(length);
        while character.is_ascii_alphanumeric() || is_in(character, URI_CHARACTERS) {
            if character == '%' {
                chunks.push_str(&self.reader.prefix(length));
                self.forward(length);
                length = 0;
                chunks.push_str(&self.scan_uri_escapes(name)?);
                any_chunk = true;
            } else {
                length += 1;
            }
            character = self.peek(length);
        }
        if length > 0 {
            chunks.push_str(&self.reader.prefix(length));
            self.forward(length);
            any_chunk = true;
        }
        if !any_chunk {
            return Err(scanner_error(
                format!("expected URI, but found {}", char_repr(character)),
                self.mark(),
            ));
        }
        Ok(chunks)
    }

    fn scan_uri_escapes(&mut self, name: &str) -> Scan<String> {
        let _ = name;
        let mut codes: Vec<u8> = Vec::new();
        let mark: Mark = self.mark();
        while self.peek(0) == '%' {
            self.forward(1);
            for offset in 0..2 {
                if !is_in(self.peek(offset), HEX_DIGITS) {
                    return Err(scanner_error(
                        format!(
                            "expected URI escape sequence of 2 hexadecimal numbers, but found {}",
                            char_repr(self.peek(offset))
                        ),
                        self.mark(),
                    ));
                }
            }
            codes.push(
                u8::from_str_radix(&self.reader.prefix(2), 16)
                    .expect("the escape holds two hexadecimal digits"),
            );
            self.forward(2);
        }
        decode_utf8(&codes).map_err(|message| scanner_error(message, mark))
    }

    fn scan_line_break(&mut self) -> String {
        let character: char = self.peek(0);
        if is_in(character, "\r\n\u{85}") {
            if self.reader.prefix(2) == "\r\n" {
                self.forward(2);
            } else {
                self.forward(1);
            }
            return "\n".to_owned();
        }
        if is_in(character, "\u{2028}\u{2029}") {
            self.forward(1);
            return character.to_string();
        }
        String::new()
    }
}

fn escape_replacement(character: char) -> Option<char> {
    Some(match character {
        '0' => '\0',
        'a' => '\u{07}',
        'b' => '\u{08}',
        't' | '\t' => '\t',
        'n' => '\n',
        'v' => '\u{0b}',
        'f' => '\u{0c}',
        'r' => '\r',
        'e' => '\u{1b}',
        ' ' => ' ',
        '"' => '"',
        '\\' => '\\',
        '/' => '/',
        'N' => '\u{85}',
        '_' => '\u{a0}',
        'L' => '\u{2028}',
        'P' => '\u{2029}',
        _ => return None,
    })
}

fn escape_code_length(character: char) -> Option<usize> {
    match character {
        'x' => Some(2),
        'u' => Some(4),
        'U' => Some(8),
        _ => None,
    }
}
