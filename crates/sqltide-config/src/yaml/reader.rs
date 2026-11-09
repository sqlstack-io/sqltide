//! The character buffer and marks of PyYAML's `Reader` for `str` input.

use crate::yaml::errors::YamlError;

/// A position in the document: the character index and zero-based line and column.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mark {
    pub index: usize,
    pub line: usize,
    pub column: usize,
}

/// Characters that end a line for marks and line breaks.
pub(crate) const LINE_BREAKS: &str = "\r\n\u{85}\u{2028}\u{2029}";
/// A line break, the end of the stream, or blank space.
pub(crate) const BREAK_OR_BLANK_OR_END: &str = "\0 \t\r\n\u{85}\u{2028}\u{2029}";
/// A line break or the end of the stream.
pub(crate) const BREAK_OR_END: &str = "\0\r\n\u{85}\u{2028}\u{2029}";
/// A line break, the end of the stream, or a space.
pub(crate) const BREAK_OR_SPACE_OR_END: &str = "\0 \r\n\u{85}\u{2028}\u{2029}";

/// The input followed by the `\0` end-of-stream sentinel, and the current position.
pub(crate) struct Reader {
    buffer: Vec<char>,
    pointer: usize,
    line: usize,
    column: usize,
}

/// Whether PyYAML's `Reader.NON_PRINTABLE` accepts the character.
fn is_printable(character: char) -> bool {
    matches!(
        character,
        '\t' | '\n' | '\r' | ' '..='~' | '\u{85}' | '\u{a0}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}'
    ) || character >= '\u{10000}'
}

impl Reader {
    /// The reader of `text`; a character PyYAML refuses fails before any token is read.
    pub(crate) fn new(text: &str) -> Result<Self, YamlError> {
        let mut reader = Self {
            buffer: text.chars().chain(std::iter::once('\0')).collect(),
            pointer: 0,
            line: 0,
            column: 0,
        };
        if let Some(position) = reader.buffer[..reader.buffer.len() - 1]
            .iter()
            .position(|character| !is_printable(*character))
        {
            let character: char = reader.buffer[position];
            reader.forward(position);
            let mark: Mark = reader.mark();
            return Err(YamlError::internal(
                format!(
                    "unacceptable character #x{:04x}: special characters are not allowed",
                    u32::from(character)
                ),
                mark,
            ));
        }
        Ok(reader)
    }

    /// The character `index` places ahead; the sentinel past the end.
    pub(crate) fn peek(&self, index: usize) -> char {
        self.buffer
            .get(self.pointer + index)
            .copied()
            .unwrap_or('\0')
    }

    /// The next `length` characters, fewer at the end.
    pub(crate) fn prefix(&self, length: usize) -> String {
        let end: usize = (self.pointer + length).min(self.buffer.len());
        self.buffer[self.pointer.min(end)..end].iter().collect()
    }

    /// Advance `length` characters, counting lines and columns as PyYAML does.
    pub(crate) fn forward(&mut self, length: usize) {
        for _ in 0..length {
            let Some(character) = self.buffer.get(self.pointer).copied() else {
                return;
            };
            self.pointer += 1;
            let next: char = self.peek(0);
            if "\n\u{85}\u{2028}\u{2029}".contains(character) || (character == '\r' && next != '\n')
            {
                self.line += 1;
                self.column = 0;
            } else if character != '\u{feff}' {
                self.column += 1;
            }
        }
    }

    pub(crate) fn mark(&self) -> Mark {
        Mark {
            index: self.pointer,
            line: self.line,
            column: self.column,
        }
    }

    pub(crate) fn column(&self) -> usize {
        self.column
    }

    pub(crate) fn line(&self) -> usize {
        self.line
    }

    pub(crate) fn index(&self) -> usize {
        self.pointer
    }
}

/// Whether `character` is one of `set`, as Python's `ch in '...'` for one character.
pub(crate) fn is_in(character: char, set: &str) -> bool {
    set.contains(character)
}
