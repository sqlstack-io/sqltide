//! SQLTide's configurable SQL Rules: project configuration, authored-body adaptation, SQLClean
//! evaluation, the reviewed exception policy and layout-only formatting. Plain Rust with no Python
//! dependency.

pub mod bodies;
pub mod config;
pub mod engine;
pub mod evaluation;
pub mod formatting;
pub mod policy;

use sqltide_config::errors::ConfigError;
use sqltide_sqltext::sql_scan::ScanError;

/// Why a Rules operation failed, by the exception the compiler raises for it.
#[derive(Debug)]
pub enum RulesFailure {
    /// Invalid configuration, SQL adaptation or unsafe candidate: `SqlRulesError`.
    Rules(String),
    /// A header, policy or SQLClean engine refusal: `ValueError`.
    Value(String),
    /// A lexical scan failure: `SqlScanError`.
    Scan(ScanError),
    /// The project file is not valid TOML: `tomllib.TOMLDecodeError`.
    Toml(ConfigError),
    /// A filesystem probe failed other than by absence: `OSError(errno, strerror, path)`.
    Io { errno: i32, path: String },
}

impl From<ScanError> for RulesFailure {
    fn from(error: ScanError) -> Self {
        Self::Scan(error)
    }
}

/// One authored project file: its path as the compiler prints it and its text.
pub struct RuleFile {
    pub path: String,
    pub contents: Vec<char>,
    /// The POSIX path relative to the project directory, or the `ValueError` message for a file
    /// outside it, raised only when a policy needs the relative path.
    pub relative_path: Result<String, String>,
}

/// Python's `str.count(sub, start, end)` and `str.rfind(sub, start, end)` bounds for `text`.
fn python_bounds(length: usize, start: i64, end: i64) -> (usize, usize) {
    let clamp = |index: i64| -> usize {
        let length = i64::try_from(length).unwrap_or(i64::MAX);
        let index = if index < 0 { index + length } else { index };
        usize::try_from(index.clamp(0, length)).unwrap_or(0)
    };
    (clamp(start), clamp(end))
}

/// The one-based line and column of character offset `position`, as the Python compiler
/// computed them with `count("\n", 0, position) + 1` and `position - rfind("\n", 0, position)`.
pub(crate) fn line_and_column(contents: &[char], position: i64) -> (i64, i64) {
    let (start, end) = python_bounds(contents.len(), 0, position);
    let window: &[char] = if start <= end {
        &contents[start..end]
    } else {
        &[]
    };
    let newlines = window
        .iter()
        .filter(|character| **character == '\n')
        .count();
    let last = window
        .iter()
        .rposition(|character| *character == '\n')
        .map_or(-1, |index| i64::try_from(start + index).unwrap_or(i64::MAX));
    (
        i64::try_from(newlines).unwrap_or(i64::MAX) + 1,
        position - last,
    )
}

#[cfg(test)]
mod tests {
    use super::line_and_column;

    #[test]
    fn line_and_column_follow_python_slice_bounds() {
        let contents: Vec<char> = "ab\ncd\né".chars().collect();
        assert_eq!(line_and_column(&contents, 0), (1, 1));
        assert_eq!(line_and_column(&contents, 4), (2, 2));
        assert_eq!(line_and_column(&contents, 7), (3, 2));
        assert_eq!(line_and_column(&contents, 99), (3, 94));
        // A negative end counts from the end, as a Python slice does.
        assert_eq!(line_and_column(&contents, -2), (2, -4));
    }
}
