//! Python string semantics the configuration contract depends on.

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

/// Python `str.isspace` for one character: Unicode whitespace plus the four information
/// separators U+001C to U+001F.
#[must_use]
pub fn is_python_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python `str.strip()`.
#[must_use]
pub fn python_strip(text: &str) -> &str {
    text.trim_matches(is_python_space)
}

/// The value of a Unicode decimal digit, as Python's `\d` and `int()` read it.
#[must_use]
pub fn decimal_digit(character: char) -> Option<u32> {
    let code: u32 = u32::from(character);
    let position: usize = DIGIT_ZEROS.partition_point(|&zero| zero <= code);
    let zero: u32 = DIGIT_ZEROS[position.checked_sub(1)?];
    (code - zero < 10).then_some(code - zero)
}

/// A run of one or more Unicode decimal digits as an integer, `None` when it is not one or
/// does not fit in `u64`.
#[must_use]
pub fn decimal_digits(text: &str) -> Option<u64> {
    if text.is_empty() {
        return None;
    }
    text.chars().try_fold(0_u64, |total, character| {
        total
            .checked_mul(10)?
            .checked_add(u64::from(decimal_digit(character)?))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn follows_python_whitespace_and_digits() {
        assert_eq!(python_strip("\u{1f} a \u{3000}"), "a");
        assert_eq!(python_strip("\u{200b}a"), "\u{200b}a");
        assert_eq!(decimal_digits("٢٠"), Some(20));
        assert_eq!(decimal_digits("1²"), None);
        assert_eq!(decimal_digits(""), None);
        assert_eq!(decimal_digit('/'), None);
        assert_eq!(decimal_digit('9'), Some(9));
        assert_eq!(decimal_digits("99999999999999999999"), None);
    }
}
