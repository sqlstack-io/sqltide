//! SQL audit queries. The audit query sits on its own lines so a trailing line comment cannot
//! swallow the closing parenthesis.

/// The number of rows an audit query returns.
#[must_use]
pub fn audit_count_query(query: &str) -> String {
    format!("SELECT count() AS failing_row_count FROM (\n{query}\n) AS __sqltide_audit")
}

/// Up to `limit` failing rows of an audit query.
#[must_use]
pub fn audit_sample_query(query: &str, limit: usize) -> String {
    format!("SELECT * FROM (\n{query}\n) AS __sqltide_audit LIMIT {limit}")
}
