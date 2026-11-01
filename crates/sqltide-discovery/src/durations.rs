//! Compact `<digits><d|h|m|s>` durations, digits being any Unicode decimal digits.

use std::cmp::Ordering;

use crate::errors::{ErrorKind, SpecError, SpecResult};
use crate::text::{decimal_digit, decimal_digits};

/// Seconds per duration unit.
#[must_use]
pub const fn unit_seconds(unit: char) -> Option<u64> {
    match unit {
        'd' => Some(24 * 60 * 60),
        'h' => Some(60 * 60),
        'm' => Some(60),
        's' => Some(1),
        _ => None,
    }
}

/// `(digits, unit seconds)` when `text` is exactly one or more decimal digits and a unit.
#[must_use]
pub fn split_duration(text: &str) -> Option<(&str, u64)> {
    let unit: char = text.chars().next_back()?;
    let digits: &str = &text[..text.len() - unit.len_utf8()];
    let seconds: u64 = unit_seconds(unit)?;
    (!digits.is_empty() && digits.chars().all(|c| decimal_digit(c).is_some()))
        .then_some((digits, seconds))
}

/// The seconds of `<digits><unit>` text, `None` when it is not one or exceeds `u64`.
#[must_use]
pub fn duration_seconds(text: &str) -> Option<u64> {
    let (digits, unit) = split_duration(text)?;
    decimal_digits(digits)?.checked_mul(unit)
}

/// Compare two valid duration texts exactly, however many digits they have.
#[must_use]
pub fn compare_durations(left: &str, right: &str) -> Option<Ordering> {
    let exact = |text: &str| -> Option<Vec<u8>> {
        let (digits, unit) = split_duration(text)?;
        let mut product: Vec<u8> = Vec::new();
        let mut carry: u64 = 0;
        let values: Vec<u64> = digits
            .chars()
            .rev()
            .map(|c| u64::from(decimal_digit(c).unwrap_or_default()))
            .collect();
        for digit in values {
            let step: u64 = digit * unit + carry;
            product.push(u8::try_from(step % 10).unwrap_or_default());
            carry = step / 10;
        }
        while carry > 0 {
            product.push(u8::try_from(carry % 10).unwrap_or_default());
            carry /= 10;
        }
        while product.len() > 1 && product.last() == Some(&0) {
            product.pop();
        }
        product.reverse();
        Some(product)
    };
    let (left, right) = (exact(left)?, exact(right)?);
    Some(left.len().cmp(&right.len()).then_with(|| left.cmp(&right)))
}

/// Python `parse_duration_seconds`: `text` is `None` when the value is not a string.
///
/// # Errors
///
/// Returns a duration error naming `field_path`.
pub fn parse_duration_seconds(
    text: Option<&str>,
    field_path: &str,
    allow_zero: bool,
) -> SpecResult<u64> {
    let seconds: u64 = text.and_then(duration_seconds).ok_or_else(|| {
        SpecError::new(
            ErrorKind::DurationParse,
            format!("{field_path} must be a duration such as '30s', '5m', '2h', or '1d'"),
        )
    })?;
    if seconds == 0 && !allow_zero {
        return Err(SpecError::new(
            ErrorKind::DurationParse,
            format!("{field_path} must be greater than zero"),
        ));
    }
    Ok(seconds)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_compares_durations() {
        assert_eq!(duration_seconds("2h"), Some(7200));
        assert_eq!(duration_seconds("٣m"), Some(180));
        assert_eq!(duration_seconds("2H"), None);
        assert_eq!(duration_seconds("h"), None);
        assert_eq!(
            compare_durations("99999999999999999999999s", "1d"),
            Some(Ordering::Greater)
        );
        assert_eq!(compare_durations("0060m", "1h"), Some(Ordering::Equal));
        assert_eq!(
            parse_duration_seconds(Some("0s"), "f", false)
                .unwrap_err()
                .message,
            "f must be greater than zero"
        );
    }
}
