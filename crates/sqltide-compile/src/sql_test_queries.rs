//! The SQL a SQL test step compares: an authored query wrapped in the CTEs it may reference,
//! and the actual and typed expected queries of one model target.

/// `query` preceded by a `WITH` clause of `(name, query)` CTEs, or unchanged without CTEs.
#[must_use]
pub fn wrap_with_ctes(query: &str, ctes: &[(&str, &str)]) -> String {
    if ctes.is_empty() {
        return query.to_owned();
    }
    let rendered: Vec<String> = ctes
        .iter()
        .map(|(name, cte_query)| format!("{name} AS (\n{cte_query}\n)"))
        .collect();
    format!("WITH\n{}\n{query}", rendered.join(",\n"))
}

/// The actual and typed expected queries of one model target: the expected columns selected
/// from the assembled model CTE, and the expected query with each column cast to the model's
/// type for it. A later output column with the same name decides its type.
///
/// # Errors
///
/// Returns the expected columns the model does not produce, in expected order.
pub fn chain_step_queries(
    expected_column_names: &[&str],
    output_columns: &[(&str, &str)],
    actual_cte_name: &str,
    expected_query: &str,
) -> Result<(String, String), Vec<String>> {
    let column_type = |name: &str| {
        output_columns
            .iter()
            .rev()
            .find(|(column, _)| *column == name)
            .map(|(_, column_type)| *column_type)
    };
    let missing: Vec<String> = expected_column_names
        .iter()
        .filter(|name| column_type(name).is_none())
        .map(|name| (*name).to_owned())
        .collect();
    if !missing.is_empty() {
        return Err(missing);
    }
    let casts: Vec<String> = expected_column_names
        .iter()
        .map(|name| {
            let column_type: &str = column_type(name).unwrap_or_default();
            format!("    CAST({name} AS {column_type}) AS {name}")
        })
        .collect();
    Ok((
        format!(
            "SELECT {} FROM {actual_cte_name}",
            expected_column_names.join(", ")
        ),
        format!(
            "SELECT\n{}\nFROM (\n{expected_query}\n) AS expected_source",
            casts.join(",\n")
        ),
    ))
}
