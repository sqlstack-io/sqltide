//! Classify each desired object against the object that exists now.
//!
//! Tables compare columns by name, ClickHouse-normalized type, and default, plus storage.
//! Materialized views and views also match when their queries generate the same canonical SQL.
//! Comparisons short-circuit in authored column order, so a type is only normalized when every
//! earlier comparison matched, and a type that cannot be normalized fails the plan only then.

use std::collections::{BTreeMap, HashMap, HashSet};

use sqltide_sqlanalysis::{SqlDialect, canonicalize_sql, normalize_data_type};

use crate::rebuild::{ChangeType, SchemaChangeKind, SeedCompatibility};

const DIALECT: &str = "clickhouse";

/// One comparable column.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Column {
    pub name: String,
    pub type_sql: String,
    pub default: Option<String>,
}

/// Engine settings, compared as a mapping; `None` differs from an empty mapping.
pub type Settings = Option<BTreeMap<String, String>>;

/// Managed table storage.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Storage {
    pub engine: String,
    pub order_by: Vec<String>,
    pub partition_by: Option<String>,
    pub ttl: Option<String>,
    pub settings: Settings,
}

/// Kafka engine settings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Kafka {
    pub broker_list: String,
    pub topic: String,
    pub consumer_group: String,
    pub format: String,
    pub settings: Settings,
}

/// One object's comparable specification.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Spec {
    Table {
        columns: Vec<Column>,
        storage: Storage,
    },
    KafkaTable {
        columns: Vec<Column>,
        kafka: Kafka,
    },
    MaterializedView {
        source: String,
        target: String,
        query: String,
    },
    View {
        query: String,
    },
}

/// How a table's columns changed, by name.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ColumnDifference {
    TypeChange,
    AddOnly,
    RemoveOnly,
    AddAndRemove,
}

impl ColumnDifference {
    #[must_use]
    pub const fn schema_change_kind(self) -> SchemaChangeKind {
        match self {
            Self::AddOnly => SchemaChangeKind::NonBreaking,
            Self::TypeChange | Self::RemoveOnly | Self::AddAndRemove => SchemaChangeKind::Breaking,
        }
    }

    #[must_use]
    pub const fn seed_compatibility(self) -> SeedCompatibility {
        match self {
            Self::TypeChange => SeedCompatibility::NonSeedable,
            Self::AddOnly | Self::RemoveOnly | Self::AddAndRemove => SeedCompatibility::Seedable,
        }
    }
}

/// A type that could not be normalized, with the message the plan fails with.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiffError {
    pub message: String,
}

impl std::fmt::Display for DiffError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for DiffError {}

/// One desired object's classification.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Classification {
    pub change_type: ChangeType,
    pub column_difference: Option<ColumnDifference>,
}

/// One desired object to classify: its spec, the existing spec if any, and whether it is
/// forced to rebuild by a full refresh or a start time.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Comparison {
    pub desired: Spec,
    pub actual: Option<Spec>,
    pub forced: bool,
}

/// Normalized types and canonical queries, computed once per text.
pub struct Differ {
    dialect: SqlDialect,
    types: HashMap<String, Result<String, String>>,
    queries: HashMap<String, Option<String>>,
}

impl Default for Differ {
    fn default() -> Self {
        Self::new()
    }
}

impl Differ {
    #[must_use]
    pub fn new() -> Self {
        Self {
            dialect: SqlDialect::new(DIALECT),
            types: HashMap::new(),
            queries: HashMap::new(),
        }
    }

    /// Classify every comparison in order.
    ///
    /// # Errors
    ///
    /// Returns the first type that cannot be normalized.
    pub fn classify_all(
        &mut self,
        comparisons: &[Comparison],
    ) -> Result<Vec<Classification>, DiffError> {
        comparisons
            .iter()
            .map(|comparison| self.classify(comparison))
            .collect()
    }

    /// Classify one desired object.
    ///
    /// # Errors
    ///
    /// Returns the first type that cannot be normalized.
    pub fn classify(&mut self, comparison: &Comparison) -> Result<Classification, DiffError> {
        let Some(actual) = &comparison.actual else {
            return Ok(Classification {
                change_type: ChangeType::Create,
                column_difference: None,
            });
        };
        let change_type: ChangeType =
            if comparison.forced || !self.specs_equal(&comparison.desired, actual)? {
                ChangeType::Rebuild
            } else {
                ChangeType::NoOp
            };
        Ok(Classification {
            change_type,
            column_difference: self.column_difference(&comparison.desired, actual)?,
        })
    }

    /// Whether the two specs describe the same object.
    ///
    /// # Errors
    ///
    /// Returns the first type that cannot be normalized.
    pub fn specs_equal(&mut self, desired: &Spec, actual: &Spec) -> Result<bool, DiffError> {
        Ok(match (desired, actual) {
            (
                Spec::Table { columns, storage },
                Spec::Table {
                    columns: actual_columns,
                    storage: actual_storage,
                },
            ) => self.columns_match(columns, actual_columns)? && storage == actual_storage,
            (
                Spec::MaterializedView {
                    source,
                    target,
                    query,
                },
                Spec::MaterializedView {
                    source: actual_source,
                    target: actual_target,
                    query: actual_query,
                },
            ) => {
                source == actual_source
                    && target == actual_target
                    && self.queries_equal(query, actual_query)
            }
            (
                Spec::View { query },
                Spec::View {
                    query: actual_query,
                },
            ) => self.queries_equal(query, actual_query),
            (desired, actual) => desired == actual,
        })
    }

    /// How a table's columns changed, or `None` when they match or either side is not a table.
    ///
    /// # Errors
    ///
    /// Returns the first shared column whose type cannot be normalized.
    pub fn column_difference(
        &mut self,
        desired: &Spec,
        actual: &Spec,
    ) -> Result<Option<ColumnDifference>, DiffError> {
        let (
            Spec::Table {
                columns: desired, ..
            },
            Spec::Table {
                columns: actual, ..
            },
        ) = (desired, actual)
        else {
            return Ok(None);
        };
        let desired_by_name: HashMap<&str, &Column> = by_name(desired);
        let actual_by_name: HashMap<&str, &Column> = by_name(actual);
        let added: bool = desired_by_name
            .keys()
            .any(|name| !actual_by_name.contains_key(name));
        let removed: bool = actual_by_name
            .keys()
            .any(|name| !desired_by_name.contains_key(name));
        let mut changed: bool = false;
        let mut seen: HashSet<&str> = HashSet::new();
        for column in desired {
            if !seen.insert(column.name.as_str()) {
                continue;
            }
            if let Some(actual_column) = actual_by_name.get(column.name.as_str()) {
                let desired_column: &Column = desired_by_name[column.name.as_str()];
                changed |= !self.column_matches(desired_column, actual_column)?;
            }
        }
        Ok(match (changed, added, removed) {
            (false, false, false) => None,
            (true, _, _) => Some(ColumnDifference::TypeChange),
            (false, true, false) => Some(ColumnDifference::AddOnly),
            (false, false, true) => Some(ColumnDifference::RemoveOnly),
            (false, true, true) => Some(ColumnDifference::AddAndRemove),
        })
    }

    fn columns_match(&mut self, desired: &[Column], actual: &[Column]) -> Result<bool, DiffError> {
        if desired.len() != actual.len() {
            return Ok(false);
        }
        for (desired, actual) in desired.iter().zip(actual) {
            if !self.column_matches(desired, actual)? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn column_matches(&mut self, desired: &Column, actual: &Column) -> Result<bool, DiffError> {
        Ok(desired.name == actual.name
            && self.normalized_type(&desired.type_sql)?
                == self.normalized_type(&actual.type_sql)?
            && desired.default == actual.default)
    }

    fn normalized_type(&mut self, type_sql: &str) -> Result<String, DiffError> {
        self.types
            .entry(type_sql.to_owned())
            .or_insert_with(|| normalize_data_type(type_sql, DIALECT).map_err(|error| error.0))
            .clone()
            .map_err(|error| DiffError {
                message: format!("Could not normalize ClickHouse type '{type_sql}': {error}"),
            })
    }

    fn queries_equal(&mut self, desired: &str, actual: &str) -> bool {
        if desired == actual {
            return true;
        }
        match (self.canonical(desired), self.canonical(actual)) {
            (Some(desired), Some(actual)) => desired == actual,
            _ => false,
        }
    }

    fn canonical(&mut self, query: &str) -> Option<String> {
        if let Some(canonical) = self.queries.get(query) {
            return canonical.clone();
        }
        let canonical: Option<String> = canonicalize_sql(query, &self.dialect).ok();
        self.queries.insert(query.to_owned(), canonical.clone());
        canonical
    }
}

/// Columns by name, the last column with a name winning.
fn by_name(columns: &[Column]) -> HashMap<&str, &Column> {
    columns
        .iter()
        .map(|column| (column.name.as_str(), column))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn column(name: &str, type_sql: &str) -> Column {
        Column {
            name: name.to_owned(),
            type_sql: type_sql.to_owned(),
            default: None,
        }
    }

    fn table(columns: Vec<Column>) -> Spec {
        Spec::Table {
            columns,
            storage: Storage {
                engine: "MergeTree".to_owned(),
                order_by: vec!["id".to_owned()],
                partition_by: None,
                ttl: None,
                settings: None,
            },
        }
    }

    #[test]
    fn types_compare_after_normalization() {
        let mut differ: Differ = Differ::new();
        let desired: Spec = table(vec![
            column("id", "uint64"),
            column("tags", "array(string)"),
        ]);
        let actual: Spec = table(vec![
            column("id", "UInt64"),
            column("tags", "Array(String)"),
        ]);
        assert_eq!(differ.specs_equal(&desired, &actual), Ok(true));
        assert_eq!(differ.column_difference(&desired, &actual), Ok(None));
    }

    #[test]
    fn classifies_column_differences_by_name() {
        let mut differ: Differ = Differ::new();
        let actual: Spec = table(vec![column("id", "UInt64"), column("old", "String")]);
        let cases: [(Vec<Column>, ColumnDifference); 4] = [
            (
                vec![
                    column("id", "UInt64"),
                    column("old", "String"),
                    column("new", "String"),
                ],
                ColumnDifference::AddOnly,
            ),
            (vec![column("id", "UInt64")], ColumnDifference::RemoveOnly),
            (
                vec![column("id", "UInt64"), column("new", "String")],
                ColumnDifference::AddAndRemove,
            ),
            (
                vec![column("id", "String"), column("new", "String")],
                ColumnDifference::TypeChange,
            ),
        ];
        for (columns, expected) in cases {
            assert_eq!(
                differ.column_difference(&table(columns), &actual),
                Ok(Some(expected))
            );
        }
    }

    #[test]
    fn a_bad_type_fails_only_when_reached() {
        let mut differ: Differ = Differ::new();
        let actual: Spec = table(vec![column("a", "UInt64"), column("b", "Array(")]);
        // The name mismatch on the first column short-circuits before `Array(` is read.
        let renamed: Spec = table(vec![column("z", "UInt64"), column("b", "Array(")]);
        assert_eq!(differ.specs_equal(&renamed, &actual), Ok(false));
        // The shared column `b` is compared for the schema difference and fails.
        let error: DiffError = differ.column_difference(&renamed, &actual).unwrap_err();
        assert!(error.message.starts_with("Could not normalize ClickHouse type 'Array(': SQL data type could not be parsed with Polyglot:"));
    }

    #[test]
    fn views_match_on_canonical_sql() {
        let mut differ: Differ = Differ::new();
        let desired: Spec = Spec::View {
            query: "select 1 AS x".to_owned(),
        };
        let actual: Spec = Spec::View {
            query: "SELECT 1 AS x".to_owned(),
        };
        assert_eq!(differ.specs_equal(&desired, &actual), Ok(true));
        let broken: Spec = Spec::View {
            query: "SELECT (".to_owned(),
        };
        assert_eq!(differ.specs_equal(&desired, &broken), Ok(false));
    }
}
