//! Validation of adopted external source tables against the warehouse catalog before a plan or
//! build uses their declared replay columns.

use std::collections::HashMap;

/// Replay columns one adopted source declares, in the order they are checked.
pub struct AdoptedSource {
    pub table_name: String,
    pub replay_boundary_mode: String,
    pub partition_column: Option<String>,
    pub offset_column: Option<String>,
    pub timestamp_column: Option<String>,
    pub landed_at_column: Option<String>,
    pub cursor_column: Option<String>,
}

/// One catalog relation: its name and `(name, type)` columns.
pub type CatalogRelation = (String, Vec<(String, String)>);

const INTEGER_TYPES: [&str; 12] = [
    "Int8", "Int16", "Int32", "Int64", "Int128", "Int256", "UInt8", "UInt16", "UInt32", "UInt64",
    "UInt128", "UInt256",
];
const SCALAR_TYPE_WRAPPERS: [&str; 2] = ["Nullable", "LowCardinality"];

/// Reject adopted sources whose table is missing from `database`, whose declared replay columns are
/// missing or have the wrong type, whose injected replay aliases collide with existing columns, or
/// that several declarations map differently. Returns the first problem's message.
pub fn validate_adopted_sources(
    sources: &[AdoptedSource],
    relations: &[CatalogRelation],
    database: &str,
) -> Result<(), String> {
    check_mapping_consistency(sources)?;
    for source in sources {
        let table = &source.table_name;
        let columns: HashMap<&str, &str> = relations
            .iter()
            .find(|(name, _)| name == table)
            .map(|(_, columns)| {
                columns
                    .iter()
                    .map(|(name, column_type)| (name.as_str(), column_type.as_str()))
                    .collect()
            })
            .unwrap_or_default();
        if columns.is_empty() {
            return Err(format!(
                "Adopted source table '{table}' does not exist in database '{database}'"
            ));
        }
        let declared = [
            (&source.partition_column, "partition", Requirement::Integer),
            (&source.offset_column, "offset", Requirement::Integer),
            (&source.timestamp_column, "timestamp", Requirement::DateTime),
            (&source.landed_at_column, "landed_at", Requirement::DateTime),
            (&source.cursor_column, "cursor", Requirement::Integer),
        ];
        for (column, role, requirement) in declared {
            if let Some(column) = column {
                check_declared_column(&columns, table, column, role, requirement)?;
            }
        }
        let aliases = [
            ("_replay_partition", &source.partition_column),
            ("_replay_offset", &source.offset_column),
            ("_replay_timestamp", &source.timestamp_column),
            ("_replay_landed_at", &source.landed_at_column),
            ("_replay_cursor", &source.cursor_column),
        ];
        for (alias, physical) in aliases {
            let Some(physical) = physical else { continue };
            if alias != physical && columns.contains_key(alias) {
                return Err(format!(
                    "Adopted source table '{table}' already defines column '{alias}', which \
                     conflicts with the injected replay alias for source column '{physical}'"
                ));
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum Requirement {
    Integer,
    DateTime,
}

fn check_mapping_consistency(sources: &[AdoptedSource]) -> Result<(), String> {
    type Signature<'a> = [Option<&'a str>; 6];
    let mut signatures: HashMap<&str, Signature<'_>> = HashMap::new();
    for source in sources {
        let signature: Signature<'_> = [
            Some(source.replay_boundary_mode.as_str()),
            source.partition_column.as_deref(),
            source.offset_column.as_deref(),
            source.timestamp_column.as_deref(),
            source.landed_at_column.as_deref(),
            source.cursor_column.as_deref(),
        ];
        if let Some(existing) = signatures.get(source.table_name.as_str())
            && *existing != signature
        {
            return Err(format!(
                "Adopted source table '{}' has conflicting replay mappings",
                source.table_name
            ));
        }
        signatures.insert(&source.table_name, signature);
    }
    Ok(())
}

fn check_declared_column(
    columns: &HashMap<&str, &str>,
    table: &str,
    column: &str,
    role: &str,
    requirement: Requirement,
) -> Result<(), String> {
    let Some(column_type) = columns.get(column) else {
        return Err(format!(
            "Adopted source table '{table}' is missing declared {role} column '{column}'"
        ));
    };
    let scalar = unwrap_scalar_type(&column_type.replace(' ', ""));
    let compatible = match requirement {
        Requirement::Integer => INTEGER_TYPES.contains(&scalar.as_str()),
        Requirement::DateTime => is_datetime_type(&scalar),
    };
    if compatible {
        Ok(())
    } else {
        Err(format!(
            "Adopted source table '{table}' declares {role} column '{column}' with incompatible \
             type '{column_type}'"
        ))
    }
}

/// Strip `Nullable(...)` and `LowCardinality(...)` wrappers from a type with its spaces removed.
fn unwrap_scalar_type(column_type: &str) -> String {
    let mut current = column_type;
    'unwrap: loop {
        for wrapper in SCALAR_TYPE_WRAPPERS {
            if let Some(inner) = current
                .strip_prefix(wrapper)
                .and_then(|rest| rest.strip_prefix('('))
                .and_then(|rest| rest.strip_suffix(')'))
            {
                current = inner;
                continue 'unwrap;
            }
        }
        return current.to_owned();
    }
}

fn is_datetime_type(column_type: &str) -> bool {
    matches!(column_type, "DateTime" | "DateTime64")
        || (column_type.starts_with("DateTime(") || column_type.starts_with("DateTime64("))
            && column_type.ends_with(')')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(table: &str, columns: [Option<&str>; 5]) -> AdoptedSource {
        let [partition, offset, timestamp, landed_at, cursor] =
            columns.map(|c| c.map(str::to_owned));
        AdoptedSource {
            table_name: table.to_owned(),
            replay_boundary_mode: "offsets".to_owned(),
            partition_column: partition,
            offset_column: offset,
            timestamp_column: timestamp,
            landed_at_column: landed_at,
            cursor_column: cursor,
        }
    }

    fn relation(name: &str, columns: &[(&str, &str)]) -> CatalogRelation {
        (
            name.to_owned(),
            columns
                .iter()
                .map(|(name, column_type)| ((*name).to_owned(), (*column_type).to_owned()))
                .collect(),
        )
    }

    #[test]
    fn unwraps_nested_wrappers_but_keeps_unbalanced_ones() {
        assert_eq!(
            unwrap_scalar_type("LowCardinality(Nullable(UInt8))"),
            "UInt8"
        );
        assert_eq!(unwrap_scalar_type("Nullable()"), "");
        assert_eq!(unwrap_scalar_type("Nullable(Int8"), "Nullable(Int8");
        assert_eq!(
            unwrap_scalar_type("Array(Nullable(Int8))"),
            "Array(Nullable(Int8))"
        );
    }

    #[test]
    fn checks_types_after_removing_spaces_and_reports_the_authored_type() {
        let relations = [relation(
            "orders",
            &[
                ("p", "Nullable( Int32 )"),
                ("t", "DateTime64(3, 'UTC')"),
                ("o", "Float64"),
            ],
        )];
        let ok = source("orders", [Some("p"), None, Some("t"), None, None]);
        assert_eq!(validate_adopted_sources(&[ok], &relations, "db"), Ok(()));
        let bad = source("orders", [Some("p"), Some("o"), None, None, None]);
        assert_eq!(
            validate_adopted_sources(&[bad], &relations, "db"),
            Err(
                "Adopted source table 'orders' declares offset column 'o' with incompatible \
                 type 'Float64'"
                    .to_owned()
            )
        );
    }

    #[test]
    fn a_table_without_columns_counts_as_missing_and_the_first_relation_wins() {
        let relations = [
            relation("orders", &[]),
            relation("orders", &[("p", "Int8")]),
        ];
        let declared = source("orders", [Some("p"), None, None, None, None]);
        assert_eq!(
            validate_adopted_sources(&[declared], &relations, "db"),
            Err("Adopted source table 'orders' does not exist in database 'db'".to_owned())
        );
    }

    #[test]
    fn conflicting_mappings_are_reported_before_any_catalog_check() {
        let first = source("missing", [Some("p"), None, None, None, None]);
        let second = source("missing", [Some("q"), None, None, None, None]);
        assert_eq!(
            validate_adopted_sources(&[first, second], &[], "db"),
            Err("Adopted source table 'missing' has conflicting replay mappings".to_owned())
        );
    }
}
