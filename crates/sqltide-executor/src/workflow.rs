//! Shared pieces of warehouse workflows: statements, step IDs, and literal SQL fragments.

use sqltide_sqltext::python_text::{python_isalnum, python_isspace};

/// Where a statement runs in a workflow.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Phase {
    Preflight,
    Preparation,
    Teardown,
    Realization,
    Stabilization,
    Boundary,
    Replay,
    Audit,
    Finalization,
}

impl Phase {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Preflight => "preflight",
            Self::Preparation => "preparation",
            Self::Teardown => "teardown",
            Self::Realization => "realization",
            Self::Stabilization => "stabilization",
            Self::Boundary => "boundary",
            Self::Replay => "replay",
            Self::Audit => "audit",
            Self::Finalization => "finalization",
        }
    }
}

/// What a statement does.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Intent {
    Assertion,
    Query,
    Mutation,
    Wait,
}

impl Intent {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Assertion => "assertion",
            Self::Query => "query",
            Self::Mutation => "mutation",
            Self::Wait => "wait",
        }
    }
}

/// One workflow statement. Its sequence is its position, counting from one.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Statement {
    pub step_id: String,
    pub phase: Phase,
    pub intent: Intent,
    pub sql: String,
    pub continue_on_error: bool,
}

impl Statement {
    #[must_use]
    pub fn new(step_id: impl Into<String>, phase: Phase, intent: Intent, sql: String) -> Self {
        Self {
            step_id: step_id.into(),
            phase,
            intent,
            sql,
            continue_on_error: false,
        }
    }

    /// A mutation whose SQL is terminated with exactly one semicolon.
    #[must_use]
    pub fn mutation(step_id: impl Into<String>, phase: Phase, sql: &str) -> Self {
        Self::new(step_id, phase, Intent::Mutation, terminate_sql(sql))
    }
}

/// `value` with every character Python's `str.isalnum` rejects replaced by `_`.
#[must_use]
pub fn step_segment(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if python_isalnum(character) {
                character
            } else {
                '_'
            }
        })
        .collect()
}

/// `sql` without trailing whitespace and semicolons, then one semicolon.
#[must_use]
pub fn terminate_sql(sql: &str) -> String {
    format!(
        "{};",
        sql.trim_end_matches(python_isspace).trim_end_matches(';')
    )
}

/// `value` escaped for a single-quoted ClickHouse string literal.
#[must_use]
pub fn escape_literal(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\'', "\\'")
}

/// The statement that sleeps for `seconds`, one row per whole second.
#[must_use]
pub fn wait_sql(seconds: f64) -> String {
    let ceiling: f64 = seconds.ceil();
    #[allow(clippy::cast_possible_truncation)]
    let wait_rows: i64 = if ceiling.is_finite() && ceiling > 1.0 {
        ceiling as i64
    } else {
        1
    };
    #[allow(clippy::cast_precision_loss)]
    let seconds_per_row: f64 = seconds / wait_rows as f64;
    format!(
        "SELECT sleepEachRow({}) FROM numbers({wait_rows}) SETTINGS max_block_size = 1;",
        format_general(seconds_per_row)
    )
}

/// `value` as Python's `format(value, "g")` renders it: six significant digits, trailing
/// zeros removed, scientific notation outside `1e-4 <= |value| < 1e6`.
#[must_use]
pub fn format_general(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if value == 0.0 {
        return if value.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    let scientific: String = format!("{value:.5e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .unwrap_or((scientific.as_str(), "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    if (-4..6).contains(&exponent) {
        let decimals: usize = usize::try_from(5 - exponent).unwrap_or(0);
        return strip_fraction(&format!("{value:.decimals$}"));
    }
    let sign: char = if exponent < 0 { '-' } else { '+' };
    format!(
        "{}e{sign}{:02}",
        strip_fraction(mantissa),
        exponent.unsigned_abs()
    )
}

fn strip_fraction(text: &str) -> String {
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        text.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_floats_like_python_general_format() {
        let cases: [(f64, &str); 12] = [
            (1.0, "1"),
            (0.5, "0.5"),
            (2.5 / 3.0, "0.833333"),
            (10.0 / 3.0, "3.33333"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (123_456.0, "123456"),
            (1_234_567.0, "1.23457e+06"),
            (999_999.5, "1e+06"),
            (0.000_123_456_7, "0.000123457"),
            (1e100, "1e+100"),
            (0.0, "0"),
        ];
        for (value, expected) in cases {
            assert_eq!(format_general(value), expected, "{value}");
        }
    }

    #[test]
    fn waits_one_row_per_started_second() {
        assert_eq!(
            wait_sql(2.5),
            "SELECT sleepEachRow(0.833333) FROM numbers(3) SETTINGS max_block_size = 1;"
        );
        assert_eq!(
            wait_sql(0.0),
            "SELECT sleepEachRow(0) FROM numbers(1) SETTINGS max_block_size = 1;"
        );
    }

    #[test]
    fn segments_terminates_and_escapes() {
        assert_eq!(step_segment("orders.v2-é"), "orders_v2_é");
        assert_eq!(terminate_sql("SELECT 1;; \n"), "SELECT 1;");
        assert_eq!(escape_literal("it's a \\ path"), "it\\'s a \\\\ path");
    }
}
