//! Which relations a deployment diff compares, the SQL that counts their rows, and how each
//! comparison is summarized.

use std::collections::{BTreeSet, HashMap};

use crate::janitor::Deployment;
use crate::publications::Binding;

/// The endpoint naming the current stable bindings.
pub const ACTIVE_ENDPOINT: &str = "active";

/// One logical relation and the physical relation each endpoint binds it to.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RelationPair {
    pub database: String,
    pub logical_name: String,
    pub from_physical_name: Option<String>,
    pub to_physical_name: Option<String>,
}

/// The summary of one relation comparison.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Status {
    Added,
    Removed,
    Changed,
    Unchanged,
    PhysicalMissing,
}

impl Status {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Added => "added",
            Self::Removed => "removed",
            Self::Changed => "changed",
            Self::Unchanged => "unchanged",
            Self::PhysicalMissing => "physical_missing",
        }
    }
}

/// `(from, to)` endpoints of `DEPLOYMENT` (compared with the active bindings) or `FROM:TO`.
///
/// # Errors
///
/// Returns the refusal message for any other shape or for equal endpoints.
pub fn parse_comparison(comparison: &str) -> Result<(String, String), String> {
    let parts: Vec<&str> = comparison.split(':').collect();
    let (from, to): (&str, &str) = match parts.as_slice() {
        [to] if !to.is_empty() => (ACTIVE_ENDPOINT, to),
        [from, to] if !from.is_empty() && !to.is_empty() => (from, to),
        _ => return Err("deployment diff expects DEPLOYMENT or FROM:TO".to_owned()),
    };
    if from == to {
        return Err("deployment diff endpoints must be different".to_owned());
    }
    Ok((from.to_owned(), to.to_owned()))
}

fn endpoint_relations(
    endpoint: &str,
    deployments: &[Deployment],
    active: &[Binding],
    default_database: &str,
) -> Result<HashMap<(String, String), String>, String> {
    if endpoint == ACTIVE_ENDPOINT {
        if active.is_empty() {
            return Err("deployment diff endpoint 'active' has no stable bindings".to_owned());
        }
        return Ok(active
            .iter()
            .map(|(database, logical_name, physical_name)| {
                (
                    (database.clone(), logical_name.clone()),
                    physical_name.clone(),
                )
            })
            .collect());
    }
    let deployment: &Deployment = deployments
        .iter()
        .find(|deployment| deployment.deployment_id == endpoint)
        .ok_or_else(|| format!("Unknown deployment diff endpoint '{endpoint}'"))?;
    if deployment.incomplete {
        return Err(format!(
            "Deployment diff endpoint '{endpoint}' is incomplete"
        ));
    }
    let relations: HashMap<(String, String), String> = deployment
        .mappings
        .iter()
        .filter(|mapping| mapping.publishes)
        .map(|mapping| {
            (
                (
                    mapping
                        .database
                        .as_deref()
                        .unwrap_or(default_database)
                        .to_owned(),
                    mapping.logical_name.clone(),
                ),
                mapping.physical_name.clone(),
            )
        })
        .collect();
    if relations.is_empty() {
        return Err(format!(
            "Deployment diff endpoint '{endpoint}' has no model relations"
        ));
    }
    Ok(relations)
}

/// Every logical relation either endpoint binds, sorted by `(database, logical name)`, and the
/// sorted databases whose catalogs the comparison reads. A later binding of the same name wins.
///
/// # Errors
///
/// Returns the refusal message for the first endpoint, `from` then `to`, that cannot be
/// resolved.
pub fn relation_pairs(
    from: &str,
    to: &str,
    deployments: &[Deployment],
    active: &[Binding],
    default_database: &str,
) -> Result<(Vec<RelationPair>, Vec<String>), String> {
    let mut from_relations = endpoint_relations(from, deployments, active, default_database)?;
    let mut to_relations = endpoint_relations(to, deployments, active, default_database)?;
    let names: BTreeSet<(String, String)> = from_relations
        .keys()
        .chain(to_relations.keys())
        .cloned()
        .collect();
    let databases: Vec<String> = names
        .iter()
        .map(|(database, _)| database.clone())
        .collect::<BTreeSet<String>>()
        .into_iter()
        .collect();
    let pairs: Vec<RelationPair> = names
        .into_iter()
        .map(|name| RelationPair {
            from_physical_name: from_relations.remove(&name),
            to_physical_name: to_relations.remove(&name),
            database: name.0,
            logical_name: name.1,
        })
        .collect();
    Ok((pairs, databases))
}

/// Whether `value` is an identifier a diff may interpolate unquoted between backticks.
#[must_use]
pub fn is_valid_identifier(value: &str) -> bool {
    let mut characters = value.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|character| character.is_ascii_alphanumeric() || character == '_')
}

/// The refusal for an identifier a diff cannot query, when `value` is not valid.
#[must_use]
pub fn identifier_error(value: &str) -> Option<String> {
    (!is_valid_identifier(value))
        .then(|| format!("Deployment diff cannot query invalid identifier '{value}'"))
}

/// The query counting the rows of `database.relation`.
///
/// # Errors
///
/// Returns the refusal message for the first invalid identifier, database then relation.
pub fn count_sql(database: &str, relation: &str) -> Result<String, String> {
    for identifier in [database, relation] {
        if let Some(error) = identifier_error(identifier) {
            return Err(error);
        }
    }
    Ok(format!(
        "SELECT count() AS row_count FROM `{database}`.`{relation}`"
    ))
}

/// One endpoint's side of a comparison: whether it binds the relation, and when the relation
/// exists, its `(name, type, default)` columns and row count.
pub type Side<'a> = (
    bool,
    Option<(&'a [(String, String, Option<String>)], Option<i128>)>,
);

/// The summary of one comparison.
#[must_use]
pub fn status(from: Side<'_>, to: Side<'_>) -> Status {
    let (from_bound, from_found) = from;
    let (to_bound, to_found) = to;
    if (from_bound && from_found.is_none()) || (to_bound && to_found.is_none()) {
        return Status::PhysicalMissing;
    }
    if !from_bound {
        return Status::Added;
    }
    if !to_bound {
        return Status::Removed;
    }
    if from_found != to_found {
        return Status::Changed;
    }
    Status::Unchanged
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::janitor::Mapping;

    fn mapping(database: Option<&str>, publishes: bool, logical: &str, physical: &str) -> Mapping {
        Mapping {
            database: database.map(str::to_owned),
            publishes,
            logical_name: logical.to_owned(),
            physical_name: physical.to_owned(),
            logical_model_name: logical.to_owned(),
        }
    }

    fn deployment(id: &str, incomplete: bool, mappings: Vec<Mapping>) -> Deployment {
        Deployment {
            deployment_id: id.to_owned(),
            created_at: String::new(),
            incomplete,
            mappings,
        }
    }

    fn binding(database: &str, logical: &str, physical: &str) -> Binding {
        (database.to_owned(), logical.to_owned(), physical.to_owned())
    }

    #[test]
    fn parses_one_or_two_distinct_endpoints() {
        assert_eq!(
            parse_comparison("d1").unwrap(),
            ("active".to_owned(), "d1".to_owned())
        );
        assert_eq!(
            parse_comparison("d1:d2").unwrap(),
            ("d1".to_owned(), "d2".to_owned())
        );
        for invalid in ["", ":d2", "d1:", "a:b:c"] {
            assert_eq!(
                parse_comparison(invalid).unwrap_err(),
                "deployment diff expects DEPLOYMENT or FROM:TO"
            );
        }
        assert_eq!(
            parse_comparison("active").unwrap_err(),
            "deployment diff endpoints must be different"
        );
    }

    #[test]
    fn pairs_relations_across_endpoints_in_name_order() {
        let deployments: Vec<Deployment> = vec![deployment(
            "d2",
            false,
            vec![
                mapping(None, true, "orders", "orders__d2"),
                mapping(Some("b"), true, "items", "items__d2"),
                mapping(None, false, "orders_mv", "orders_mv__d2"),
            ],
        )];
        let active: Vec<Binding> = vec![
            binding("main", "orders", "orders__d1"),
            binding("main", "legacy", "legacy__d1"),
        ];
        let (pairs, databases) =
            relation_pairs("active", "d2", &deployments, &active, "main").unwrap();
        let summary: Vec<(&str, &str, Option<&str>, Option<&str>)> = pairs
            .iter()
            .map(|pair| {
                (
                    pair.database.as_str(),
                    pair.logical_name.as_str(),
                    pair.from_physical_name.as_deref(),
                    pair.to_physical_name.as_deref(),
                )
            })
            .collect();
        assert_eq!(
            summary,
            vec![
                ("b", "items", None, Some("items__d2")),
                ("main", "legacy", Some("legacy__d1"), None),
                ("main", "orders", Some("orders__d1"), Some("orders__d2")),
            ]
        );
        assert_eq!(databases, vec!["b".to_owned(), "main".to_owned()]);
    }

    #[test]
    fn refuses_unresolvable_endpoints_from_first() {
        let deployments: Vec<Deployment> = vec![
            deployment(
                "d1",
                true,
                vec![mapping(None, true, "orders", "orders__d1")],
            ),
            deployment("d2", false, vec![mapping(None, false, "mv", "mv__d2")]),
        ];
        let refusal = |from: &str, to: &str, active: &[Binding]| {
            relation_pairs(from, to, &deployments, active, "main").unwrap_err()
        };
        assert_eq!(
            refusal("active", "d9", &[]),
            "deployment diff endpoint 'active' has no stable bindings"
        );
        assert_eq!(
            refusal("d9", "d1", &[]),
            "Unknown deployment diff endpoint 'd9'"
        );
        assert_eq!(
            refusal("d1", "d9", &[]),
            "Deployment diff endpoint 'd1' is incomplete"
        );
        assert_eq!(
            refusal("d2", "d1", &[]),
            "Deployment diff endpoint 'd2' has no model relations"
        );
    }

    #[test]
    fn validates_identifiers_before_building_count_sql() {
        assert_eq!(
            count_sql("main", "orders__d1").unwrap(),
            "SELECT count() AS row_count FROM `main`.`orders__d1`"
        );
        assert_eq!(
            count_sql("ma-in", "or der").unwrap_err(),
            "Deployment diff cannot query invalid identifier 'ma-in'"
        );
        assert!(!is_valid_identifier("1orders"));
        assert!(!is_valid_identifier("ordérs"));
        assert!(is_valid_identifier("_x9"));
    }

    #[test]
    fn summarizes_comparisons() {
        let columns: Vec<(String, String, Option<String>)> =
            vec![("id".to_owned(), "UInt64".to_owned(), None)];
        let found: Side<'_> = (true, Some((&columns, Some(3))));
        assert_eq!(status((true, None), (false, None)), Status::PhysicalMissing);
        assert_eq!(status((false, None), found), Status::Added);
        assert_eq!(status(found, (false, None)), Status::Removed);
        assert_eq!(
            status(found, (true, Some((&columns, Some(4))))),
            Status::Changed
        );
        assert_eq!(status(found, (true, Some((&[], Some(3))))), Status::Changed);
        assert_eq!(status(found, found), Status::Unchanged);
    }
}
