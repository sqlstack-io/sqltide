//! Byte-size strings such as `100GiB`, read exactly as pydantic's `ByteSize` reads them.
//!
//! A string passing pydantic-core's pattern is read by Python `int()`, else by Python's
//! `^\s*(\d*\.?\d+)\s*(\w+)?` match and `int(float(scalar) * unit)`; one failing the pattern
//! can still be a lax integer such as `+5` or `+5.0`.

use std::sync::LazyLock;

use regex::Regex;

use crate::text::{decimal_digit, is_python_space};

/// pydantic-core's string pattern, matched with the same `regex` engine pydantic-core uses.
static PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^\s*(\d*\.?\d+)\s*(\w+)?").expect("the byte size pattern is valid")
});

/// A parsed size, or `TooLarge` when it does not fit in `u128`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ByteSize {
    Bytes(u128),
    TooLarge,
}

/// The size `text` denotes, `None` when pydantic rejects it.
#[must_use]
pub fn parse_byte_size(text: &str) -> Option<ByteSize> {
    if !PATTERN.is_match(text) {
        return lax_integer(text);
    }
    if let Some(value) = python_int(text) {
        return value;
    }
    let (scalar, unit) = python_match(text)?;
    let multiplier: f64 = unit_multiplier(&unit.to_lowercase())?;
    let ascii: String = scalar
        .chars()
        .map(|c| match decimal_digit(c) {
            Some(digit) => char::from_digit(digit, 10).unwrap_or(c),
            None => c,
        })
        .collect();
    let product: f64 = ascii.parse::<f64>().ok()? * multiplier;
    if !product.is_finite() {
        return None;
    }
    let truncated: f64 = product.trunc();
    // `u128::MAX` as `f64` rounds up to 2^128, so anything at or above it does not fit.
    #[allow(
        clippy::cast_precision_loss,
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss
    )]
    Some(if truncated >= u128::MAX as f64 {
        ByteSize::TooLarge
    } else {
        ByteSize::Bytes(truncated as u128)
    })
}

/// Python `int(text)` when it succeeds: `Some(None)`-like results fold into `None` for
/// failure; a negative value cannot pass the pattern, so it is not representable here.
fn python_int(text: &str) -> Option<Option<ByteSize>> {
    let trimmed: &str = text.trim_matches(int_space);
    let unsigned: &str = trimmed.strip_prefix('+').unwrap_or(trimmed);
    if unsigned.starts_with('-') {
        return None;
    }
    let digits: Vec<u32> = grouped_digits(unsigned, decimal_digit)?;
    Some(Some(from_digits(&digits)))
}

/// Whitespace Python `int()` trims: ASCII `\t\n\v\f\r ` and non-ASCII whitespace, so not `\x1c`.
fn int_space(character: char) -> bool {
    if character.is_ascii() {
        matches!(character, ' ' | '\t'..='\r')
    } else {
        is_python_space(character)
    }
}

/// Digits separated by single underscores, none leading or trailing.
fn grouped_digits(text: &str, digit: impl Fn(char) -> Option<u32>) -> Option<Vec<u32>> {
    let mut digits: Vec<u32> = Vec::new();
    let mut after_underscore: bool = false;
    for character in text.chars() {
        if character == '_' {
            if digits.is_empty() || after_underscore {
                return None;
            }
            after_underscore = true;
            continue;
        }
        digits.push(digit(character)?);
        after_underscore = false;
    }
    (!digits.is_empty() && !after_underscore).then_some(digits)
}

fn from_digits(digits: &[u32]) -> ByteSize {
    digits
        .iter()
        .try_fold(0_u128, |total, &digit| {
            total.checked_mul(10)?.checked_add(u128::from(digit))
        })
        .map_or(ByteSize::TooLarge, ByteSize::Bytes)
}

/// pydantic-core's lax integer: trimmed, an optional sign, grouped ASCII digits, and an
/// optional fraction of zeros. Any run of zeros and underscores after a leading zero is
/// dropped before the underscore rules apply, so `+0__5` is 5. Zero and negative values cannot be valid limits, so only
/// positive ones are returned.
fn lax_integer(text: &str) -> Option<ByteSize> {
    let trimmed: &str = text.trim();
    let unsigned: &str = trimmed.strip_prefix('+')?;
    let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    if unsigned.contains('.') && (fraction.is_empty() || !fraction.bytes().all(|b| b == b'0')) {
        return None;
    }
    if whole.starts_with('_') || whole.ends_with('_') {
        return None;
    }
    let significant: &str = if whole.starts_with('0') {
        whole.trim_start_matches(['0', '_'])
    } else {
        whole
    };
    if significant.is_empty() {
        return Some(ByteSize::Bytes(0));
    }
    let digits: Vec<u32> = grouped_digits(significant, |c| c.to_digit(10))?;
    Some(from_digits(&digits))
}

/// Python's `^\s*(\d*\.?\d+)\s*(\w+)?` match: the scalar and the unit, `b` when absent.
fn python_match(text: &str) -> Option<(String, String)> {
    let characters: Vec<char> = text.chars().collect();
    let mut position: usize = characters
        .iter()
        .take_while(|&&c| is_python_space(c))
        .count();
    let start: usize = position;
    let digits_at = |from: usize| {
        characters[from..]
            .iter()
            .take_while(|&&c| decimal_digit(c).is_some())
            .count()
    };
    let whole: usize = digits_at(position);
    let after_whole: usize = position + whole;
    let fraction: usize = if characters.get(after_whole) == Some(&'.') {
        digits_at(after_whole + 1)
    } else {
        0
    };
    position = if fraction > 0 {
        after_whole + 1 + fraction
    } else if whole > 0 {
        after_whole
    } else {
        return None;
    };
    let scalar: String = characters[start..position].iter().collect();
    position += characters[position..]
        .iter()
        .take_while(|&&c| is_python_space(c))
        .count();
    let unit: String = characters[position..]
        .iter()
        .take_while(|&&c| c.is_alphanumeric() || c == '_')
        .collect();
    Some((
        scalar,
        if unit.is_empty() {
            "b".to_owned()
        } else {
            unit
        },
    ))
}

#[allow(clippy::cast_precision_loss)]
fn unit_multiplier(unit: &str) -> Option<f64> {
    let decimal = |power: i32| 10_f64.powi(power);
    let binary = |power: i32| 2_f64.powi(power);
    Some(match unit {
        "b" => 1.0,
        "kb" | "k" => decimal(3),
        "mb" | "m" => decimal(6),
        "gb" | "g" => decimal(9),
        "tb" | "t" => decimal(12),
        "pb" | "p" => decimal(15),
        "eb" | "e" => decimal(18),
        "kib" => binary(10),
        "mib" => binary(20),
        "gib" => binary(30),
        "tib" => binary(40),
        "pib" => binary(50),
        "eib" => binary(60),
        "bit" => 1.0 / 8.0,
        "kbit" => decimal(3) / 8.0,
        "mbit" => decimal(6) / 8.0,
        "gbit" => decimal(9) / 8.0,
        "tbit" => decimal(12) / 8.0,
        "pbit" => decimal(15) / 8.0,
        "ebit" => decimal(18) / 8.0,
        "kibit" => binary(10) / 8.0,
        "mibit" => binary(20) / 8.0,
        "gibit" => binary(30) / 8.0,
        "tibit" => binary(40) / 8.0,
        "pibit" => binary(50) / 8.0,
        "eibit" => binary(60) / 8.0,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bytes(text: &str) -> Option<u128> {
        match parse_byte_size(text)? {
            ByteSize::Bytes(value) => Some(value),
            ByteSize::TooLarge => None,
        }
    }

    #[test]
    fn reads_sizes_as_pydantic_does() {
        let cases: [(&str, Option<u128>); 17] = [
            ("100GiB", Some(107_374_182_400)),
            ("1.5KiB", Some(1536)),
            (".5kb", Some(500)),
            ("5.", Some(5)),
            ("1_000", Some(1000)),
            ("10GiB!!", Some(10_737_418_240)),
            ("10 GiBx", None),
            ("1e3", None),
            ("١٠GiB", Some(10_737_418_240)),
            ("10bit", Some(1)),
            ("+5.00", Some(5)),
            ("+5.0_0", None),
            ("\u{1c}5", None),
            ("5\u{1c}", Some(5)),
            ("1_67\u{1c}", None),
            ("+0__89 ", Some(89)),
            ("+5__0", None),
        ];
        for (text, expected) in cases {
            assert_eq!(bytes(text), expected, "{text:?}");
        }
    }
}
