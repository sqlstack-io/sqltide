//! Split MODEL header text into words, quoted strings and structural symbols.

use crate::python_text::python_isspace;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum TokenKind {
    End,
    Word,
    Text,
    Symbol,
}

#[derive(Clone, Debug)]
pub(super) struct Token {
    pub(super) kind: TokenKind,
    pub(super) value: String,
    pub(super) position: usize,
}

const KEY_VALUE_SEPARATOR: char = ':';
const ESCAPE: char = '\\';

fn is_symbol(character: char) -> bool {
    matches!(character, '(' | ')' | '[' | ']' | ',')
}

fn is_brace(character: char) -> bool {
    matches!(character, '{' | '}')
}

fn quote_name(character: char) -> Option<&'static str> {
    match character {
        '\'' => Some("single"),
        '"' => Some("double"),
        _ => None,
    }
}

pub(super) fn tokenize(characters: &[char], sets: bool) -> Result<Vec<Token>, String> {
    let mut tokens: Vec<Token> = Vec::new();
    let mut index: usize = 0;
    while index < characters.len() {
        let character: char = characters[index];
        if python_isspace(character) {
            index += 1;
            continue;
        }
        if is_brace(character) && !sets {
            return Err(format!(
                "YAML-like brace mappings are not supported at position {index}; use SQLBuild \
                 parenthesized mapping syntax"
            ));
        }
        if is_symbol(character) || is_brace(character) || character == KEY_VALUE_SEPARATOR {
            tokens.push(Token {
                kind: TokenKind::Symbol,
                value: character.to_string(),
                position: index,
            });
            index += 1;
            continue;
        }
        if quote_name(character).is_some() {
            let (value, next_index) = read_quoted(characters, index)?;
            tokens.push(Token {
                kind: TokenKind::Text,
                value,
                position: index,
            });
            index = next_index;
            continue;
        }
        let next_index: usize = word_end(characters, index)?;
        tokens.push(Token {
            kind: TokenKind::Word,
            value: characters[index..next_index].iter().collect(),
            position: index,
        });
        index = next_index;
    }
    tokens.push(Token {
        kind: TokenKind::End,
        value: String::new(),
        position: characters.len(),
    });
    Ok(tokens)
}

fn word_end(characters: &[char], start: usize) -> Result<usize, String> {
    let mut index: usize = start;
    while index < characters.len() {
        let character: char = characters[index];
        if python_isspace(character)
            || is_symbol(character)
            || is_brace(character)
            || character == KEY_VALUE_SEPARATOR
        {
            break;
        }
        if let Some(name) = quote_name(character) {
            return Err(format!(
                "unexpected {name} quote inside bare value at position {index}; quote the whole \
                 value"
            ));
        }
        index += 1;
    }
    Ok(index)
}

fn read_quoted(characters: &[char], start: usize) -> Result<(String, usize), String> {
    let quote: char = characters[start];
    let mut value: String = String::new();
    let mut index: usize = start + 1;
    while index < characters.len() {
        let character: char = characters[index];
        if character == ESCAPE {
            let Some(escaped) = characters.get(index + 1) else {
                return Err(format!("unterminated escape at position {index}"));
            };
            value.push(*escaped);
            index += 2;
            continue;
        }
        if character == quote {
            return Ok((value, index + 1));
        }
        value.push(character);
        index += 1;
    }
    let name: &str = quote_name(quote).unwrap_or("double");
    Err(format!(
        "unterminated {name}-quoted string at position {start}"
    ))
}
