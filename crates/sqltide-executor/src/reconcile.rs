//! Which desired tables, and the transforms that write them, a reconcile may record as deployed.

use std::collections::HashMap;

use sqltide_planner::diff::{Column, DiffError, Differ};

const RAW_TABLE_NAME_PREFIX: &str = "raw__";
const VIEW_REJECTION: &str =
    "authored views require persisted deployment query metadata; redeploy the view";

/// `(database, object type, name)` of one object.
pub type Key = (Option<String>, String, String);

/// The parts of a managed table a reconcile compares.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Table {
    pub engine: String,
    pub order_by: Vec<String>,
    pub partition_by: Option<String>,
    pub columns: Vec<Column>,
}

/// A transform's source and target table names.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Transform {
    pub source: String,
    pub target: String,
}

/// One desired or actual object.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Object {
    Table(Key, Table),
    View(Key),
    MaterializedView(Key, Transform),
    /// An object a reconcile ignores, such as a Kafka table.
    Other,
}

/// The verdict for one target.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Outcome {
    /// Record these desired objects, by position: the table, then the transform writing it.
    Eligible {
        table: usize,
        transform: Option<usize>,
    },
    Rejected {
        name: String,
        reasons: Vec<&'static str>,
    },
}

/// A first-insertion-ordered map whose later inserts replace the value, like a Python `dict`.
struct Ordered<'a, V> {
    keys: Vec<&'a Key>,
    values: HashMap<&'a Key, V>,
}

impl<'a, V> Ordered<'a, V> {
    fn new() -> Self {
        Self {
            keys: Vec::new(),
            values: HashMap::new(),
        }
    }

    fn insert(&mut self, key: &'a Key, value: V) {
        if self.values.insert(key, value).is_none() {
            self.keys.push(key);
        }
    }
}

/// A verdict for each target: the selected keys ordered by name, or with no selection every
/// non-raw desired table and then every desired view. Selected keys with no desired table or
/// view get no verdict.
///
/// # Errors
///
/// Returns [`DiffError`] when a column type the comparison reaches cannot be normalized.
pub fn classify_targets(
    desired: &[Object],
    actual: &[Object],
    selected: &[Key],
) -> Result<Vec<(Key, Outcome)>, DiffError> {
    let mut tables: Ordered<'_, usize> = Ordered::new();
    let mut views: Ordered<'_, usize> = Ordered::new();
    for (position, object) in desired.iter().enumerate() {
        match object {
            Object::Table(key, _) if !key.2.starts_with(RAW_TABLE_NAME_PREFIX) => {
                tables.insert(key, position);
            }
            Object::View(key) => views.insert(key, position),
            _ => {}
        }
    }
    let mut transform_by_target: HashMap<&Key, usize> = HashMap::new();
    for (position, object) in desired.iter().enumerate() {
        if let Object::MaterializedView(_, transform) = object
            && let Some(&target) = tables.keys.iter().find(|key| key.2 == transform.target)
        {
            transform_by_target.insert(target, position);
        }
    }
    let mut actual_tables: HashMap<&Key, &Table> = HashMap::new();
    let mut actual_transforms: HashMap<&Key, &Transform> = HashMap::new();
    for object in actual {
        match object {
            Object::Table(key, table) => {
                actual_tables.insert(key, table);
            }
            Object::MaterializedView(key, transform) => {
                actual_transforms.insert(key, transform);
            }
            _ => {}
        }
    }
    let targets: Vec<&Key> = if selected.is_empty() {
        tables.keys.iter().chain(&views.keys).copied().collect()
    } else {
        let mut targets: Vec<&Key> = selected.iter().collect();
        targets.sort_by(|left, right| left.2.cmp(&right.2));
        targets
    };
    let mut differ: Differ = Differ::new();
    let mut outcomes: Vec<(Key, Outcome)> = Vec::new();
    for target in targets {
        if views.values.contains_key(target) {
            outcomes.push((
                target.clone(),
                Outcome::Rejected {
                    name: target.2.clone(),
                    reasons: vec![VIEW_REJECTION],
                },
            ));
            continue;
        }
        let Some(&table_position) = tables.values.get(target) else {
            continue;
        };
        let Object::Table(_, desired_table) = &desired[table_position] else {
            unreachable!("table positions index desired tables");
        };
        let mut reasons: Vec<&'static str> = Vec::new();
        match actual_tables.get(target) {
            None => reasons.push("live target table not found"),
            Some(actual_table) => {
                if desired_table.engine != actual_table.engine {
                    reasons.push("engine does not match");
                }
                if desired_table.order_by != actual_table.order_by {
                    reasons.push("order_by does not match");
                }
                if desired_table.partition_by != actual_table.partition_by {
                    reasons.push("partition_by does not match");
                }
                if !differ.columns_match(&desired_table.columns, &actual_table.columns)? {
                    reasons.push("columns or types do not match");
                }
            }
        }
        let transform: Option<usize> = transform_by_target.get(target).copied();
        if let Some(position) = transform {
            let Object::MaterializedView(key, desired_transform) = &desired[position] else {
                unreachable!("transform positions index desired materialized views");
            };
            match actual_transforms.get(key) {
                None => reasons.push("live transform materialized view not found"),
                Some(actual_transform) => {
                    if desired_transform.source != actual_transform.source {
                        reasons.push("live transform source does not match");
                    }
                    if desired_transform.target != actual_transform.target {
                        reasons.push("live transform target does not match");
                    }
                }
            }
        }
        let outcome: Outcome = if reasons.is_empty() {
            Outcome::Eligible {
                table: table_position,
                transform,
            }
        } else {
            Outcome::Rejected {
                name: target.2.clone(),
                reasons,
            }
        };
        outcomes.push((target.clone(), outcome));
    }
    Ok(outcomes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(object_type: &str, name: &str) -> Key {
        (None, object_type.to_owned(), name.to_owned())
    }

    fn column(name: &str, type_sql: &str) -> Column {
        Column {
            name: name.to_owned(),
            type_sql: type_sql.to_owned(),
            default: None,
        }
    }

    fn table(columns: Vec<Column>) -> Table {
        Table {
            engine: "MergeTree".to_owned(),
            order_by: vec!["id".to_owned()],
            partition_by: None,
            columns,
        }
    }

    fn transform(source: &str, target: &str) -> Transform {
        Transform {
            source: source.to_owned(),
            target: target.to_owned(),
        }
    }

    #[test]
    fn adopts_matching_tables_with_their_transforms_and_rejects_views() {
        let desired: Vec<Object> = vec![
            Object::View(key("view", "summary")),
            Object::Table(key("table", "orders"), table(vec![column("id", "UInt64")])),
            Object::Table(key("table", "raw__orders"), table(vec![])),
            Object::MaterializedView(
                key("materialized_view", "orders_mv"),
                transform("raw__orders", "orders"),
            ),
        ];
        let actual: Vec<Object> = vec![
            Object::Table(key("table", "orders"), table(vec![column("id", "UInt64")])),
            Object::MaterializedView(
                key("materialized_view", "orders_mv"),
                transform("raw__orders", "orders"),
            ),
        ];
        let outcomes = classify_targets(&desired, &actual, &[]).unwrap();
        assert_eq!(
            outcomes,
            vec![
                (
                    key("table", "orders"),
                    Outcome::Eligible {
                        table: 1,
                        transform: Some(3)
                    }
                ),
                (
                    key("view", "summary"),
                    Outcome::Rejected {
                        name: "summary".to_owned(),
                        reasons: vec![VIEW_REJECTION]
                    }
                ),
            ]
        );
    }

    #[test]
    fn collects_every_reason_and_orders_selected_targets_by_name() {
        let mut changed: Table = table(vec![column("id", "UInt32")]);
        changed.engine = "ReplacingMergeTree".to_owned();
        changed.partition_by = Some("toDate(at)".to_owned());
        let desired: Vec<Object> = vec![
            Object::Table(key("table", "zeta"), table(vec![column("id", "UInt64")])),
            Object::Table(key("table", "alpha"), table(vec![column("id", "UInt64")])),
            Object::MaterializedView(
                key("materialized_view", "zeta_mv"),
                transform("events", "zeta"),
            ),
        ];
        let actual: Vec<Object> = vec![
            Object::Table(key("table", "zeta"), changed),
            Object::MaterializedView(
                key("materialized_view", "zeta_mv"),
                transform("other", "zeta"),
            ),
        ];
        let selected: Vec<Key> = vec![
            key("table", "zeta"),
            key("table", "missing"),
            key("table", "alpha"),
        ];
        let outcomes = classify_targets(&desired, &actual, &selected).unwrap();
        assert_eq!(
            outcomes,
            vec![
                (
                    key("table", "alpha"),
                    Outcome::Rejected {
                        name: "alpha".to_owned(),
                        reasons: vec!["live target table not found"]
                    }
                ),
                (
                    key("table", "zeta"),
                    Outcome::Rejected {
                        name: "zeta".to_owned(),
                        reasons: vec![
                            "engine does not match",
                            "partition_by does not match",
                            "columns or types do not match",
                            "live transform source does not match",
                        ],
                    }
                ),
            ]
        );
    }

    #[test]
    fn equivalent_type_spellings_match_and_unparseable_types_fail() {
        let desired: Vec<Object> = vec![Object::Table(
            key("table", "t"),
            table(vec![column("v", "Nullable(String)")]),
        )];
        let actual: Vec<Object> = vec![Object::Table(
            key("table", "t"),
            table(vec![column("v", "Nullable( String )")]),
        )];
        assert_eq!(
            classify_targets(&desired, &actual, &[]).unwrap()[0].1,
            Outcome::Eligible {
                table: 0,
                transform: None
            }
        );
        let broken: Vec<Object> = vec![Object::Table(
            key("table", "t"),
            table(vec![column("v", "Array(")]),
        )];
        let error = classify_targets(&broken, &actual, &[]).unwrap_err();
        assert!(
            error
                .message
                .starts_with("Could not normalize ClickHouse type 'Array('")
        );
    }
}
