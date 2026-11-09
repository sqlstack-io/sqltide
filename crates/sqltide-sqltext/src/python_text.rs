//! Python `str` predicates the compiler must reproduce exactly.

use crate::python_tables::{PYTHON_ALNUM, PYTHON_ALPHA};

/// Whether Python's `str.isspace` accepts `character`. Rust's `char::is_whitespace` omits the
/// four separator controls U+001C to U+001F that Python treats as whitespace.
#[must_use]
pub fn python_isspace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Whether Python's `str.isalpha` accepts `character`. Rust's `char::is_alphabetic` also accepts
/// combining marks and letter numerals that Python rejects.
#[must_use]
pub fn python_isalpha(character: char) -> bool {
    if character.is_ascii() {
        return character.is_ascii_alphabetic();
    }
    in_ranges(&PYTHON_ALPHA, character)
}

/// Whether Python's `str.isalnum` accepts `character`.
#[must_use]
pub fn python_isalnum(character: char) -> bool {
    if character.is_ascii() {
        return character.is_ascii_alphanumeric();
    }
    in_ranges(&PYTHON_ALNUM, character)
}

/// Python's `str.strip()` with no arguments.
#[must_use]
pub fn python_strip(characters: &[char]) -> &[char] {
    let start: usize = characters
        .iter()
        .position(|character| !python_isspace(*character))
        .unwrap_or(characters.len());
    let end: usize = characters
        .iter()
        .rposition(|character| !python_isspace(*character))
        .map_or(start, |index| index + 1);
    &characters[start..end]
}

/// Python's `str.upper()`, which applies full case mappings such as `ß` to `SS`.
#[must_use]
pub fn python_upper(characters: &[char]) -> String {
    characters
        .iter()
        .flat_map(|character| character.to_uppercase())
        .collect()
}

fn in_ranges(ranges: &[(u32, u32)], character: char) -> bool {
    let code_point: u32 = u32::from(character);
    ranges
        .binary_search_by(|(start, end)| {
            if code_point < *start {
                std::cmp::Ordering::Greater
            } else if code_point > *end {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}
