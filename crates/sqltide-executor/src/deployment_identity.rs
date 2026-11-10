//! The creation timestamp of a virtual deployment and the identifier derived from it. Parsing
//! follows CPython 3.12's `datetime.strptime(text, "%Y%m%dT%H%M%SZ")` on identifiers that already
//! match the deployment ID pattern, so identifiers accepted and rejected before stay so.

use sqltide_discovery::text::decimal_digit;

use crate::timestamps::{civil_from_days, days_in_month};

const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_DAY: i64 = 86_400;

/// `(year, month, day, hour, minute, second)` of a UTC instant in microseconds since the epoch.
fn civil_time(micros: i64) -> (i64, i64, i64, i64, i64, i64) {
    let seconds: i64 = micros.div_euclid(MICROS_PER_SECOND);
    let (year, month, day) = civil_from_days(seconds.div_euclid(SECONDS_PER_DAY));
    let second_of_day: i64 = seconds.rem_euclid(SECONDS_PER_DAY);
    (
        year,
        month,
        day,
        second_of_day / 3_600,
        second_of_day % 3_600 / 60,
        second_of_day % 60,
    )
}

/// `YYYY-MM-DD HH:MM:SS.mmm` of a UTC instant, truncated to milliseconds.
#[must_use]
pub fn creation_timestamp(micros: i64) -> String {
    let (year, month, day, hour, minute, second) = civil_time(micros);
    let millisecond: i64 = micros.rem_euclid(MICROS_PER_SECOND) / 1_000;
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{millisecond:03}")
}

/// `YYYYMMDDTHHMMSSZ_<suffix>` of a UTC instant.
#[must_use]
pub fn generated_deployment_id(micros: i64, suffix: &str) -> String {
    let (year, month, day, hour, minute, second) = civil_time(micros);
    format!("{year:04}{month:02}{day:02}T{hour:02}{minute:02}{second:02}Z_{suffix}")
}

/// Whether `first` and `second` match one of the two-character `strptime` alternatives, each
/// an allowed ASCII first character and an allowed second character.
fn two_digit(first: char, second: char, alternatives: &[(&str, bool)]) -> Option<i64> {
    let value: u32 = decimal_digit(second)?;
    let leading: u32 = first.to_digit(10)?;
    alternatives
        .iter()
        .any(|(allowed, any_second)| {
            allowed.contains(first) && (*any_second || second.is_ascii_digit())
        })
        .then_some(i64::from(leading * 10 + value))
}

/// The `strptime` fields of `YYYYMMDDTHHMMSSZ`, or `None` when the text does not match.
fn fields(stamp: &str) -> Option<[i64; 6]> {
    let characters: Vec<char> = stamp.chars().collect();
    if characters.len() != 16 || characters[8] != 'T' || characters[15] != 'Z' {
        return None;
    }
    let year: i64 = characters[..4].iter().try_fold(0_i64, |total, character| {
        Some(total * 10 + i64::from(decimal_digit(*character)?))
    })?;
    let pair = |position: usize, alternatives: &[(&str, bool)]| {
        two_digit(characters[position], characters[position + 1], alternatives)
    };
    // `%m` is `1[0-2]|0[1-9]|[1-9]`, `%d` is `3[01]|[12]\d|0[1-9]|[1-9]| [1-9]`, `%H` is
    // `2[0-3]|[0-1]\d|\d`, `%M` is `[0-5]\d|\d`, and `%S` is `6[0-1]|[0-5]\d|\d`, where `\d` is
    // any Unicode decimal digit and the rest are ASCII. With four digits for `%m%d` and six for
    // `%H%M%S`, every field takes exactly two characters.
    let month: i64 = pair(4, &[("1", false), ("0", false)])?;
    let day: i64 = pair(6, &[("3", false), ("12", true), ("0", false)])?;
    let hour: i64 = pair(9, &[("2", false), ("01", true)])?;
    let minute: i64 = pair(11, &[("012345", true)])?;
    let second: i64 = pair(13, &[("6", false), ("012345", true)])?;
    let in_range: bool = match characters[4] {
        '1' => (10..=12).contains(&month),
        _ => (1..=9).contains(&month),
    } && match characters[6] {
        '3' => (30..=31).contains(&day),
        '0' => (1..=9).contains(&day),
        _ => true,
    } && (characters[9] != '2' || hour <= 23)
        && (characters[13] != '6' || second <= 61);
    in_range.then_some([year, month, day, hour, minute, second])
}

/// `YYYY-MM-DD HH:MM:SS.000` of a deployment ID's timestamp, as Python 3.12's `strftime`
/// renders it on Linux, or the message of the `ValueError` its `strptime` raises.
///
/// # Errors
///
/// The timestamp does not match the format, or is not a valid UTC date and time.
pub fn created_at_from_deployment_id(deployment_id: &str) -> Result<String, String> {
    let stamp: &str = deployment_id
        .split_once('_')
        .map_or(deployment_id, |(stamp, _)| stamp);
    let [year, month, day, hour, minute, second] = fields(stamp)
        .ok_or_else(|| format!("time data '{stamp}' does not match format '%Y%m%dT%H%M%SZ'"))?;
    if year < 1 {
        return Err(format!("year {year} is out of range"));
    }
    if day > days_in_month(year, month) {
        return Err("day is out of range for month".to_owned());
    }
    if second > 59 {
        return Err("second must be in 0..59".to_owned());
    }
    Ok(format!(
        "{year}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.000"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn follows_python_strptime() {
        assert_eq!(
            created_at_from_deployment_id("20260802T120000Z_reviewed").as_deref(),
            Ok("2026-08-02 12:00:00.000")
        );
        assert_eq!(
            created_at_from_deployment_id("２０２６0101T0１0000Z_a").as_deref(),
            Ok("2026-01-01 01:00:00.000")
        );
        assert_eq!(
            created_at_from_deployment_id("09990101T000000Z_a").as_deref(),
            Ok("999-01-01 00:00:00.000")
        );
        for (rejected, message) in [
            (
                "2026010１T000000Z_a",
                "time data '2026010１T000000Z' does not match format '%Y%m%dT%H%M%SZ'",
            ),
            ("20260230T000060Z_a", "day is out of range for month"),
            ("20261231T235960Z_a", "second must be in 0..59"),
            ("00000230T000060Z_a", "year 0 is out of range"),
        ] {
            assert_eq!(
                created_at_from_deployment_id(rejected),
                Err(message.to_owned()),
                "{rejected}"
            );
        }
    }

    #[test]
    fn renders_instants() {
        let micros: i64 = 1_786_017_600_123_456;
        assert_eq!(creation_timestamp(micros), "2026-08-06 12:00:00.123");
        assert_eq!(
            generated_deployment_id(micros, "abc123"),
            "20260806T120000Z_abc123"
        );
    }
}
