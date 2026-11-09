//! Configuration values read from TOML, with the value domain of Python's `tomllib`.

use crate::constants::{
    FEBRUARY, MAX_HOUR, MAX_MICROSECOND, MAX_MINUTE, MAX_SECOND, MAX_YEAR, MIN_YEAR,
    MONTHS_PER_YEAR,
};

/// A calendar date, Python's `datetime.date`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ConfigDate {
    pub year: u16,
    pub month: u8,
    pub day: u8,
}

/// A time of day with microseconds, Python's naive `datetime.time`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ConfigTime {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
}

/// A date and time, Python's `datetime.datetime`, with its UTC offset in seconds when aware.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ConfigDateTime {
    pub date: ConfigDate,
    pub time: ConfigTime,
    pub utc_offset_seconds: Option<i32>,
}

impl ConfigDate {
    /// The date, or `None` where Python's `datetime.date` constructor raises.
    pub fn checked(year: i64, month: i64, day: i64) -> Option<Self> {
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
        let month_days = match month {
            FEBRUARY => 28 + i64::from(leap),
            4 | 6 | 9 | 11 => 30,
            _ => 31,
        };
        let valid = (MIN_YEAR..=MAX_YEAR).contains(&year)
            && (1..=MONTHS_PER_YEAR).contains(&month)
            && (1..=month_days).contains(&day);
        valid.then_some(Self {
            year: year as u16,
            month: month as u8,
            day: day as u8,
        })
    }
}

impl ConfigTime {
    /// The time, or `None` where Python's `datetime.time` constructor raises.
    pub fn checked(hour: i64, minute: i64, second: i64, microsecond: i64) -> Option<Self> {
        let valid = (0..=MAX_HOUR).contains(&hour)
            && (0..=MAX_MINUTE).contains(&minute)
            && (0..=MAX_SECOND).contains(&second)
            && (0..=MAX_MICROSECOND).contains(&microsecond);
        valid.then_some(Self {
            hour: hour as u8,
            minute: minute as u8,
            second: second as u8,
            microsecond: microsecond as u32,
        })
    }
}

/// One loaded configuration value; tables keep `tomllib`'s key order.
#[derive(Clone, Debug, PartialEq)]
pub enum ConfigValue {
    Bool(bool),
    Integer(i64),
    Float(f64),
    String(String),
    Date(ConfigDate),
    DateTime(ConfigDateTime),
    Time(ConfigTime),
    List(Vec<ConfigValue>),
    /// A table in `tomllib`'s key order.
    Map(Vec<(String, ConfigValue)>),
}
