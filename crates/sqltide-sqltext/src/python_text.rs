//! Python `str` predicates the compiler must reproduce exactly.

use crate::python_tables::{PYTHON_ALNUM, PYTHON_ALPHA, PYTHON_NONPRINTABLE};

/// The digit-zero code point of every Unicode decimal digit run (Unicode 15.0, as Python 3.12).
const DIGIT_ZEROS: [u32; 68] = [
    0x00030, 0x00660, 0x006F0, 0x007C0, 0x00966, 0x009E6, 0x00A66, 0x00AE6, 0x00B66, 0x00BE6,
    0x00C66, 0x00CE6, 0x00D66, 0x00DE6, 0x00E50, 0x00ED0, 0x00F20, 0x01040, 0x01090, 0x017E0,
    0x01810, 0x01946, 0x019D0, 0x01A80, 0x01A90, 0x01B50, 0x01BB0, 0x01C40, 0x01C50, 0x0A620,
    0x0A8D0, 0x0A900, 0x0A9D0, 0x0A9F0, 0x0AA50, 0x0ABF0, 0x0FF10, 0x104A0, 0x10D30, 0x11066,
    0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650, 0x116C0, 0x11730, 0x118E0,
    0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60, 0x16AC0, 0x16B50, 0x1D7CE, 0x1D7D8,
    0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E950, 0x1FBF0,
];

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

/// The value of a Unicode decimal digit, as Python's `\d` and `int()` read it.
#[must_use]
pub fn decimal_digit(character: char) -> Option<u32> {
    let code: u32 = u32::from(character);
    let position: usize = DIGIT_ZEROS.partition_point(|&zero| zero <= code);
    let zero: u32 = DIGIT_ZEROS[position.checked_sub(1)?];
    (code - zero < 10).then_some(code - zero)
}

/// Whether Python's `str.isprintable` accepts `character`.
#[must_use]
pub fn python_isprintable(character: char) -> bool {
    if character.is_ascii() {
        return (' '..='~').contains(&character);
    }
    !in_ranges(&PYTHON_NONPRINTABLE, character)
}

/// Python's `repr()` of a `str`.
#[must_use]
pub fn python_repr(text: &str) -> String {
    let quote: char = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut rendered: String = String::with_capacity(text.len() + 2);
    rendered.push(quote);
    for character in text.chars() {
        match character {
            '\\' => rendered.push_str("\\\\"),
            '\t' => rendered.push_str("\\t"),
            '\n' => rendered.push_str("\\n"),
            '\r' => rendered.push_str("\\r"),
            _ if character == quote => {
                rendered.push('\\');
                rendered.push(character);
            }
            _ if python_isprintable(character) => rendered.push(character),
            _ => {
                let code: u32 = u32::from(character);
                let escape: String = if code <= 0xff {
                    format!("\\x{code:02x}")
                } else if code <= 0xffff {
                    format!("\\u{code:04x}")
                } else {
                    format!("\\U{code:08x}")
                };
                rendered.push_str(&escape);
            }
        }
    }
    rendered.push(quote);
    rendered
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
