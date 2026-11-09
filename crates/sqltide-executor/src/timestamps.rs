//! Timestamps as SQLTide reads them from warehouse metadata and the command line, and the UTC
//! arithmetic done on them. Parsing follows CPython 3.12's `datetime.fromisoformat` and
//! `datetime.strptime` exactly, so values accepted and rejected before stay so.

use std::sync::LazyLock;

use regex::Regex;

const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_DAY: i64 = 86_400;
/// Days from 0001-01-01 to 1970-01-01; Python's ordinal of the epoch is one more.
const EPOCH_ORDINAL: i64 = 719_163;
const MIN_YEAR: i64 = 1;
const MAX_YEAR: i64 = 9_999;

/// Why a timestamp is unusable. `Value` and `Overflow` correspond to Python's `ValueError` and
/// `OverflowError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TimestampError {
    Value(String),
    Overflow(String),
}

fn out_of_range() -> TimestampError {
    TimestampError::Overflow("date value out of range".to_owned())
}

/// Days since 1970-01-01 of a proleptic Gregorian date.
pub(crate) fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year: i64 = year - i64::from(month <= 2);
    let era: i64 = year.div_euclid(400);
    let year_of_era: i64 = year - era * 400;
    let day_of_year: i64 = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let day_of_era: i64 = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// The proleptic Gregorian date of a day count since 1970-01-01.
pub(crate) fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days: i64 = days + 719_468;
    let era: i64 = days.div_euclid(146_097);
    let day_of_era: i64 = days - era * 146_097;
    let year_of_era: i64 =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year: i64 = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index: i64 = (5 * day_of_year + 2) / 153;
    let day: i64 = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month: i64 = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    (year_of_era + era * 400 + i64::from(month <= 2), month, day)
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// A UTC instant within years 1 to 9999, in microseconds since the Unix epoch.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Instant(i64);

impl Instant {
    fn from_micros(micros: i64) -> Result<Self, TimestampError> {
        let lowest: i64 = days_from_civil(MIN_YEAR, 1, 1) * SECONDS_PER_DAY * MICROS_PER_SECOND;
        let highest: i64 =
            (days_from_civil(MAX_YEAR, 12, 31) + 1) * SECONDS_PER_DAY * MICROS_PER_SECOND - 1;
        if (lowest..=highest).contains(&micros) {
            Ok(Self(micros))
        } else {
            Err(out_of_range())
        }
    }

    /// This instant moved by whole seconds, as `datetime + timedelta(seconds=...)`.
    pub fn plus_seconds(self, seconds: i64) -> Result<Self, TimestampError> {
        seconds
            .checked_mul(MICROS_PER_SECOND)
            .and_then(|delta| self.0.checked_add(delta))
            .ok_or_else(out_of_range)
            .and_then(Self::from_micros)
    }

    /// The start of the `cadence_seconds` slot holding this instant, computed as
    /// `int(now.timestamp()) // cadence * cadence` and read back in UTC.
    pub fn cadence_slot(self, cadence_seconds: i64) -> Result<Self, TimestampError> {
        // `timestamp()` is the correctly rounded float of the exact seconds, then truncated.
        let sign: &str = if self.0 < 0 { "-" } else { "" };
        let magnitude: u64 = self.0.unsigned_abs();
        let seconds: f64 = format!(
            "{sign}{}.{:06}",
            magnitude / MICROS_PER_SECOND as u64,
            magnitude % MICROS_PER_SECOND as u64
        )
        .parse()
        .unwrap_or(0.0);
        let whole: i64 = seconds.trunc() as i64;
        // Python's `//` rounds toward negative infinity for either sign.
        let quotient: i64 = whole / cadence_seconds;
        let floored: i64 = if whole % cadence_seconds != 0 && (whole < 0) != (cadence_seconds < 0) {
            quotient - 1
        } else {
            quotient
        };
        let slot: i64 = floored * cadence_seconds;
        slot.checked_mul(MICROS_PER_SECOND)
            .ok_or_else(out_of_range)
            .and_then(Self::from_micros)
            .map_err(|_| {
                let (year, _, _) = civil_from_days(slot.div_euclid(SECONDS_PER_DAY));
                TimestampError::Value(format!("year {year} is out of range"))
            })
    }

    /// `YYYY-MM-DD HH:MM:SS.mmm`, the warehouse's millisecond rendering.
    pub fn render(self) -> String {
        let seconds: i64 = self.0.div_euclid(MICROS_PER_SECOND);
        let micros: i64 = self.0.rem_euclid(MICROS_PER_SECOND);
        let (year, month, day) = civil_from_days(seconds.div_euclid(SECONDS_PER_DAY));
        let clock: i64 = seconds.rem_euclid(SECONDS_PER_DAY);
        format!(
            "{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02}.{:03}",
            clock / 3_600,
            clock % 3_600 / 60,
            clock % 60,
            micros / 1_000
        )
    }
}

/// Wall-clock fields and the UTC offset a timestamp was written with.
struct Civil {
    year: i64,
    month: i64,
    day: i64,
    hour: i64,
    minute: i64,
    second: i64,
    microsecond: i64,
}

impl Civil {
    /// Python's `datetime(...)` argument checks, in its order.
    fn checked(self) -> Result<Self, TimestampError> {
        let invalid = |message: &str| Err(TimestampError::Value(message.to_owned()));
        if !(MIN_YEAR..=MAX_YEAR).contains(&self.year) {
            return Err(TimestampError::Value(format!(
                "year {} is out of range",
                self.year
            )));
        }
        if !(1..=12).contains(&self.month) {
            return invalid("month must be in 1..12");
        }
        if self.day < 1 || self.day > days_in_month(self.year, self.month) {
            return invalid("day is out of range for month");
        }
        if !(0..=23).contains(&self.hour) {
            return invalid("hour must be in 0..23");
        }
        if !(0..=59).contains(&self.minute) {
            return invalid("minute must be in 0..59");
        }
        if !(0..=59).contains(&self.second) {
            return invalid("second must be in 0..59");
        }
        if !(0..=999_999).contains(&self.microsecond) {
            return invalid("microsecond must be in 0..999999");
        }
        Ok(self)
    }

    fn micros(&self) -> i64 {
        ((days_from_civil(self.year, self.month, self.day) * SECONDS_PER_DAY
            + self.hour * 3_600
            + self.minute * 60
            + self.second)
            * MICROS_PER_SECOND)
            + self.microsecond
    }
}

/// Python's `repr()` of a string, for error messages.
fn python_repr(text: &str) -> String {
    let quote: char = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut repr: String = String::from(quote);
    for character in text.chars() {
        match character {
            '\\' => repr.push_str("\\\\"),
            '\n' => repr.push_str("\\n"),
            '\r' => repr.push_str("\\r"),
            '\t' => repr.push_str("\\t"),
            character if character == quote => {
                repr.push('\\');
                repr.push(character);
            }
            character
                if (character as u32) < 0x20 || (0x7f..0xa0).contains(&(character as u32)) =>
            {
                repr.push_str(&format!("\\x{:02x}", character as u32));
            }
            character => repr.push(character),
        }
    }
    repr.push(quote);
    repr
}

/// The byte at `index`, or NUL past the end, as CPython reads its NUL-terminated buffer.
fn byte(bytes: &[u8], index: usize) -> u8 {
    bytes.get(index).copied().unwrap_or(0)
}

fn is_digit(value: u8) -> bool {
    value.is_ascii_digit()
}

/// `parse_digits`: read exactly `count` ASCII digits into `value`.
fn parse_digits(bytes: &[u8], position: &mut usize, value: &mut i64, count: usize) -> bool {
    for _ in 0..count {
        let digit: u8 = byte(bytes, *position);
        *position += 1;
        if !is_digit(digit) {
            return false;
        }
        *value = *value * 10 + i64::from(digit - b'0');
    }
    true
}

/// `iso_to_ymd`: an ISO year, week and weekday as a calendar date.
fn iso_to_ymd(iso_year: i64, iso_week: i64, iso_day: i64) -> Option<(i64, i64, i64)> {
    if !(MIN_YEAR..=MAX_YEAR).contains(&iso_year) {
        return None;
    }
    let first_weekday: i64 = (days_from_civil(iso_year, 1, 1) + EPOCH_ORDINAL + 6) % 7;
    if iso_week <= 0 || iso_week >= 53 {
        let long_year: bool =
            iso_week == 53 && (first_weekday == 3 || (first_weekday == 2 && is_leap(iso_year)));
        if !long_year {
            return None;
        }
    }
    if !(1..=7).contains(&iso_day) {
        return None;
    }
    let first_day: i64 = days_from_civil(iso_year, 1, 1);
    let mut week_one_monday: i64 = first_day - first_weekday;
    if first_weekday > 3 {
        week_one_monday += 7;
    }
    Some(civil_from_days(
        week_one_monday + (iso_week - 1) * 7 + iso_day - 1,
    ))
}

/// `parse_isoformat_date`: the date before the separator, or `None` when malformed.
fn parse_isoformat_date(bytes: &[u8], length: usize) -> Option<(i64, i64, i64)> {
    let mut position: usize = 0;
    let mut year: i64 = 0;
    if !parse_digits(bytes, &mut position, &mut year, 4) {
        return None;
    }
    let uses_separator: bool = byte(bytes, position) == b'-';
    if uses_separator {
        position += 1;
    }
    if byte(bytes, position) == b'W' {
        position += 1;
        let mut iso_week: i64 = 0;
        if !parse_digits(bytes, &mut position, &mut iso_week, 2) {
            return None;
        }
        let mut iso_day: i64 = 0;
        if position < length {
            if uses_separator {
                let separator: u8 = byte(bytes, position);
                position += 1;
                if separator != b'-' {
                    return None;
                }
            }
            if !parse_digits(bytes, &mut position, &mut iso_day, 1) {
                return None;
            }
        } else {
            iso_day = 1;
        }
        return iso_to_ymd(year, iso_week, iso_day);
    }
    let mut month: i64 = 0;
    if !parse_digits(bytes, &mut position, &mut month, 2) {
        return None;
    }
    if uses_separator {
        let separator: u8 = byte(bytes, position);
        position += 1;
        if separator != b'-' {
            return None;
        }
    }
    let mut day: i64 = 0;
    if !parse_digits(bytes, &mut position, &mut day, 2) {
        return None;
    }
    Some((year, month, day))
}

/// `parse_hh_mm_ss_ff` over `bytes[start..end]`: `Err` when malformed, otherwise the fields
/// and whether anything other than NUL follows them.
fn parse_hh_mm_ss_ff(bytes: &[u8], start: usize, end: usize) -> Result<([i64; 4], bool), ()> {
    let mut fields: [i64; 4] = [0; 4];
    let mut position: usize = start;
    let mut has_separator: bool = true;
    for index in 0..3 {
        if !parse_digits(bytes, &mut position, &mut fields[index], 2) {
            return Err(());
        }
        let separator: u8 = byte(bytes, position);
        position += 1;
        if index == 0 {
            has_separator = separator == b':';
        }
        if position >= end {
            return Ok((fields, separator != 0));
        } else if has_separator && separator == b':' {
            continue;
        } else if separator == b'.' || separator == b',' {
            break;
        } else if !has_separator {
            position -= 1;
        } else {
            return Err(());
        }
    }
    let to_parse: usize = end.saturating_sub(position).min(6);
    if !parse_digits(bytes, &mut position, &mut fields[3], to_parse) {
        return Err(());
    }
    if to_parse < 6 {
        fields[3] *= 10_i64.pow(6 - to_parse as u32);
    }
    while is_digit(byte(bytes, position)) {
        position += 1;
    }
    Ok((fields, byte(bytes, position) != 0))
}

/// A parsed time and its UTC offset as `(seconds, microseconds)` when one was written.
type IsoTime = ([i64; 4], Option<(i64, i64)>);

/// `parse_isoformat_time` over `bytes[start..]`.
fn parse_isoformat_time(bytes: &[u8], start: usize) -> Option<IsoTime> {
    let end: usize = bytes.len();
    let mut zone: usize = start;
    loop {
        if matches!(byte(bytes, zone), b'Z' | b'+' | b'-') {
            break;
        }
        zone += 1;
        if zone >= end {
            break;
        }
    }
    let (time, trailing) = parse_hh_mm_ss_ff(bytes, start, zone).ok()?;
    if zone == end {
        return if trailing { None } else { Some((time, None)) };
    }
    if byte(bytes, zone) == b'Z' {
        return if byte(bytes, zone + 1) != 0 {
            None
        } else {
            Some((time, Some((0, 0))))
        };
    }
    let sign: i64 = if byte(bytes, zone) == b'-' { -1 } else { 1 };
    let (offset, trailing) = parse_hh_mm_ss_ff(bytes, zone + 1, end).ok()?;
    if trailing {
        return None;
    }
    Some((
        time,
        Some((
            sign * (offset[0] * 3_600 + offset[1] * 60 + offset[2]),
            sign * offset[3],
        )),
    ))
}

/// `_find_isoformat_datetime_separator`: the byte index of the date/time separator.
fn datetime_separator(bytes: &[u8]) -> Option<usize> {
    let length: usize = bytes.len();
    if length == 7 {
        return Some(7);
    }
    if byte(bytes, 4) == b'-' {
        if byte(bytes, 5) != b'W' {
            return Some(10);
        }
        if length < 8 {
            return None;
        }
        if length > 8 && byte(bytes, 8) == b'-' {
            if length == 9 {
                return None;
            }
            if length > 10 && is_digit(byte(bytes, 10)) {
                return Some(8);
            }
            return Some(10);
        }
        return Some(8);
    }
    if byte(bytes, 4) == b'W' {
        let mut index: usize = 7;
        while index < length && is_digit(byte(bytes, index)) {
            index += 1;
        }
        if index < 9 {
            return Some(index);
        }
        return Some(if index.is_multiple_of(2) { 7 } else { 8 });
    }
    Some(8)
}

/// Python's `timedelta.__repr__` of a normalized offset.
fn timedelta_repr(seconds: i64, microseconds: i64) -> String {
    let total: i64 = seconds * MICROS_PER_SECOND + microseconds;
    let days: i64 = total.div_euclid(SECONDS_PER_DAY * MICROS_PER_SECOND);
    let rest: i64 = total.rem_euclid(SECONDS_PER_DAY * MICROS_PER_SECOND);
    let parts: Vec<String> = [
        ("days", days),
        ("seconds", rest / MICROS_PER_SECOND),
        ("microseconds", rest % MICROS_PER_SECOND),
    ]
    .into_iter()
    .filter(|(_, value)| *value != 0)
    .map(|(name, value)| format!("{name}={value}"))
    .collect();
    if parts.is_empty() {
        "datetime.timedelta(0)".to_owned()
    } else {
        format!("datetime.timedelta({})", parts.join(", "))
    }
}

/// `datetime.fromisoformat(text)` as wall-clock fields and the written UTC offset in
/// microseconds, if any.
fn from_isoformat(text: &str) -> Result<(Civil, Option<i64>), TimestampError> {
    let invalid =
        || TimestampError::Value(format!("Invalid isoformat string: {}", python_repr(text)));
    if text.chars().count() < 7 {
        return Err(invalid());
    }
    let bytes: &[u8] = text.as_bytes();
    let separator: usize = datetime_separator(bytes).ok_or_else(invalid)?;
    let (year, month, day) = parse_isoformat_date(bytes, separator).ok_or_else(invalid)?;
    let (time, offset) = if bytes.len() > separator {
        let lead: u8 = bytes[separator];
        let width: usize = if lead & 0x80 == 0 {
            1
        } else {
            match lead & 0xf0 {
                0xe0 => 3,
                0xf0 => 4,
                _ => 2,
            }
        };
        parse_isoformat_time(bytes, separator + width).ok_or_else(invalid)?
    } else {
        ([0; 4], None)
    };
    let offset: Option<i64> = match offset {
        None | Some((0, _)) => offset.map(|_| 0),
        Some((seconds, microseconds)) => {
            let total: i64 = seconds * MICROS_PER_SECOND + microseconds;
            if total.abs() >= SECONDS_PER_DAY * MICROS_PER_SECOND {
                return Err(TimestampError::Value(format!(
                    "offset must be a timedelta strictly between -timedelta(hours=24) and \
                     timedelta(hours=24), not {}.",
                    timedelta_repr(seconds, microseconds)
                )));
            }
            Some(total)
        }
    };
    let civil: Civil = Civil {
        year,
        month,
        day,
        hour: time[0],
        minute: time[1],
        second: time[2],
        microsecond: time[3],
    }
    .checked()?;
    Ok((civil, offset))
}

/// A warehouse timestamp read as `datetime.fromisoformat` after spaces become `T` and `Z`
/// becomes `+00:00`; naive values are UTC and aware ones are converted to UTC.
pub fn parse_warehouse_timestamp(text: &str) -> Result<Instant, TimestampError> {
    let normalized: String = text.replace(' ', "T").replace('Z', "+00:00");
    let (civil, offset) = from_isoformat(&normalized)?;
    match offset {
        None => Ok(Instant(civil.micros())),
        Some(offset) => Instant::from_micros(civil.micros() - offset),
    }
}

/// The `--start-time` formats in the order they are tried, with `strptime`'s field patterns.
static START_TIME_PATTERNS: LazyLock<Vec<(bool, Regex)>> = LazyLock::new(|| {
    const YEAR: &str = r"(?P<Y>[0-9][0-9][0-9][0-9])";
    const MONTH: &str = r"(?P<m>1[0-2]|0[1-9]|[1-9])";
    const DAY: &str = r"(?P<d>3[0-1]|[1-2][0-9]|0[1-9]|[1-9]| [1-9])";
    const CLOCK: &str =
        r"(?P<H>2[0-3]|[0-1][0-9]|[0-9]):(?P<M>[0-5][0-9]|[0-9]):(?P<S>6[0-1]|[0-5][0-9]|[0-9])";
    const FRACTION: &str = r"\.(?P<f>[0-9]{1,6})";
    let date: String = format!("{YEAR}-{MONTH}-{DAY}");
    [
        (true, date.clone()),
        (false, format!("{date}T{CLOCK}")),
        (false, format!("{date}T{CLOCK}{FRACTION}")),
        (false, format!("{date}T{CLOCK}Z")),
        (false, format!("{date}T{CLOCK}{FRACTION}Z")),
    ]
    .into_iter()
    .map(|(date_only, pattern)| {
        (
            date_only,
            Regex::new(&format!("(?i)^{pattern}")).expect("the start-time patterns are valid"),
        )
    })
    .collect()
});

/// A `--start-time` value as a ClickHouse millisecond timestamp, or `None` when it matches no
/// accepted format. Dates mean midnight UTC.
pub fn normalize_start_time(raw: &str) -> Option<String> {
    START_TIME_PATTERNS.iter().find_map(|(date_only, pattern)| {
        let captures = pattern.captures(raw)?;
        if captures.get(0)?.end() != raw.len() {
            return None;
        }
        let number = |name: &str| -> i64 {
            captures
                .name(name)
                .map_or(0, |value| value.as_str().trim().parse().unwrap_or(0))
        };
        let microsecond: i64 = captures.name("f").map_or(0, |value| {
            format!("{:0<6}", value.as_str()).parse().unwrap_or(0)
        });
        let civil: Civil = Civil {
            year: number("Y"),
            month: number("m"),
            day: number("d"),
            hour: number("H"),
            minute: number("M"),
            second: number("S"),
            microsecond,
        }
        .checked()
        .ok()?;
        Some(if *date_only {
            format!(
                "{:04}-{:02}-{:02} 00:00:00.000",
                civil.year, civil.month, civil.day
            )
        } else {
            Instant(civil.micros()).render()
        })
    })
}
