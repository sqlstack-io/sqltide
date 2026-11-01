//! Bidirectional bag comparisons between expected and actual rows, with NULL-safe equality.

use crate::CapabilityError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetDifferenceTarget {
    pub column_names: Vec<String>,
    pub ctes: Vec<(String, String)>,
    pub actual_query: String,
    pub expected_query: Option<String>,
}

/// Render every target's missing and unexpected rows as one `UNION ALL` query.
///
/// # Errors
///
/// Returns a capability error when there are no targets.
pub fn render_set_difference_comparison(
    targets: &[SetDifferenceTarget],
) -> Result<String, CapabilityError> {
    if targets.is_empty() {
        return Err(CapabilityError(
            "ClickHouse comparison requires at least one target".to_owned(),
        ));
    }
    Ok(targets
        .iter()
        .enumerate()
        .map(|(index, target)| format!("(\n{}\n)", render_target(target, index)))
        .collect::<Vec<_>>()
        .join("\nUNION ALL\n"))
}

fn render_target(target: &SetDifferenceTarget, index: usize) -> String {
    let expected_counts = format!("__sqltide_expected_counts_{index}");
    let actual_counts = format!("__sqltide_actual_counts_{index}");
    let columns = &target.column_names;
    let mut ctes: Vec<String> = target
        .ctes
        .iter()
        .map(|(name, query)| format!("{name} AS (\n{query}\n)"))
        .collect();
    if let Some(expected_query) = &target.expected_query {
        ctes.push(grouped_counts(&expected_counts, expected_query, columns));
    }
    ctes.push(grouped_counts(
        &actual_counts,
        &target.actual_query,
        columns,
    ));
    let comparison = match &target.expected_query {
        None => {
            let row_values = row_values("actual_rows", columns);
            format!(
                "SELECT {index} AS _case_index, 'unexpected' AS _diff_type,\n       {row_values} \
                 AS _row_values,\n       actual_rows.__sqltide_multiplicity AS _multiplicity\nFROM \
                 {actual_counts} AS actual_rows"
            )
        }
        Some(_) => format!(
            "{}\nUNION ALL\n{}",
            directional_rows(
                index,
                "missing",
                ("expected_rows", &expected_counts),
                ("actual_rows", &actual_counts),
                columns
            ),
            directional_rows(
                index,
                "unexpected",
                ("actual_rows", &actual_counts),
                ("expected_rows", &expected_counts),
                columns
            )
        ),
    };
    format!("WITH\n{}\n{comparison}", ctes.join(",\n"))
}

fn grouped_counts(name: &str, query: &str, column_names: &[String]) -> String {
    let columns = column_names.join(", ");
    format!(
        "{name} AS (\nSELECT {columns}, count() AS __sqltide_multiplicity\nFROM (\n{query}\n)\nGROUP \
         BY {columns}\n)"
    )
}

fn directional_rows(
    index: usize,
    diff_type: &str,
    (primary_alias, primary_name): (&str, &str),
    (secondary_alias, secondary_name): (&str, &str),
    column_names: &[String],
) -> String {
    let row_values = row_values(primary_alias, column_names);
    let join_conditions: Vec<String> = column_names
        .iter()
        .map(|column| {
            format!("isNotDistinctFrom({primary_alias}.{column}, {secondary_alias}.{column})")
        })
        .collect();
    format!(
        "SELECT {index} AS _case_index, '{diff_type}' AS _diff_type,\n       {row_values} AS \
         _row_values,\n       {primary_alias}.__sqltide_multiplicity - \
         ifNull({secondary_alias}.__sqltide_multiplicity, 0) AS _multiplicity\nFROM {primary_name} \
         AS {primary_alias}\nALL LEFT JOIN {secondary_name} AS {secondary_alias}\nON {}\nWHERE \
         {primary_alias}.__sqltide_multiplicity > ifNull({secondary_alias}.__sqltide_multiplicity, 0)",
        join_conditions.join(" AND ")
    )
}

fn row_values(alias: &str, column_names: &[String]) -> String {
    let values: Vec<String> = column_names
        .iter()
        .map(|column| format!("CAST({alias}.{column} AS Nullable(String))"))
        .collect();
    format!("[{}]", values.join(", "))
}
