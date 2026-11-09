//! Character checks applied before parsing.

use crate::errors::{ConfigError, ConfigErrorKind};

/// Whether YAML allows `character` in a stream (`c-printable`).
fn is_printable(character: char) -> bool {
    matches!(
        character,
        '\t' | '\n' | '\r' | ' '..='~' | '\u{85}' | '\u{a0}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}'
    ) || character >= '\u{10000}'
}

/// Reject characters YAML does not allow, with PyYAML's message, at PyYAML's line and column.
pub(crate) fn check_characters(text: &str) -> Result<(), ConfigError> {
    let Some((index, character)) = text
        .char_indices()
        .find(|(_, character)| !is_printable(*character))
    else {
        return Ok(());
    };
    let (mut line, mut column): (usize, usize) = (1, 1);
    let mut characters = text[..index].chars().peekable();
    while let Some(character) = characters.next() {
        let breaks: bool = matches!(character, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
            || (character == '\r' && characters.peek() != Some(&'\n'));
        if breaks {
            line += 1;
            column = 1;
        } else if character != '\u{feff}' {
            column += 1;
        }
    }
    Err(ConfigError::new(
        ConfigErrorKind::Syntax,
        format!(
            "unacceptable character #x{:04x}: special characters are not allowed",
            u32::from(character)
        ),
    )
    .at(line, column))
}
