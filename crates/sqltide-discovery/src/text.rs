//! Python string semantics the configuration contract depends on.

pub use sqltide_sqltext::python_text::decimal_digit;

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
