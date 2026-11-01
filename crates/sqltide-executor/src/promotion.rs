//! The stable bindings a publish writes, the stale bindings it removes, and its live effects.

use std::collections::{HashMap, HashSet};

use crate::janitor::{Deployment, Mapping};
use crate::publications::Binding;

/// One staged relation a publish may bind.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Staged {
    /// `None` means the default database.
    pub database: Option<String>,
    pub object_type: String,
    pub logical_name: String,
    pub physical_name: String,
}

impl Staged {
    fn publishes(&self) -> bool {
        matches!(self.object_type.as_str(), "table" | "view")
    }
}

/// The live effects of replacing bindings, by position in the request.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Preview {
    /// Every binding is new and nothing is removed.
    pub initial_publish: bool,
    pub additions: Vec<usize>,
    /// `(binding, physical name it replaces)`.
    pub replacements: Vec<(usize, String)>,
    /// `(removal, physical name it unbinds)`.
    pub removals: Vec<(usize, String)>,
    /// `(database, physical name)` relations no binding points at afterwards, sorted.
    pub orphaned: Vec<(String, String)>,
}

/// Positions of the inspected candidates staged by `deployment_id` that a publish binds.
///
/// # Errors
///
/// Returns the refusal message when there are none.
pub fn inspected_candidates(
    candidates: &[(String, String)],
    deployment_id: &str,
) -> Result<Vec<usize>, String> {
    let suffix: String = format!("__{deployment_id}");
    let positions: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (physical_name, object_type))| {
            physical_name.ends_with(&suffix) && matches!(object_type.as_str(), "table" | "view")
        })
        .map(|(position, _)| position)
        .collect();
    if positions.is_empty() {
        return Err(no_relations(deployment_id));
    }
    Ok(positions)
}

/// The bindings publishing `deployment_id` writes, tables before views within each database.
/// Every relation must exist; only tables and views are bound.
///
/// # Errors
///
/// Returns the refusal message when a relation is missing or nothing can be bound.
pub fn publish_bindings(
    relations: &[Staged],
    existing: &HashSet<String>,
    default_database: &str,
    deployment_id: &str,
) -> Result<Vec<Binding>, String> {
    let mut missing: Vec<&str> = relations
        .iter()
        .map(|relation| relation.physical_name.as_str())
        .filter(|name| !existing.contains(*name))
        .collect();
    if !missing.is_empty() {
        missing.sort_unstable();
        return Err(format!(
            "Deployment '{deployment_id}' is missing staged relations: {}",
            missing.join(", ")
        ));
    }
    let mut published: Vec<&Staged> = relations
        .iter()
        .filter(|relation| relation.publishes())
        .collect();
    if published.is_empty() {
        return Err(no_relations(deployment_id));
    }
    published.sort_by(|left, right| sort_key(left).cmp(&sort_key(right)));
    Ok(published
        .into_iter()
        .map(|relation| {
            (
                relation
                    .database
                    .as_deref()
                    .unwrap_or(default_database)
                    .to_owned(),
                relation.logical_name.clone(),
                relation.physical_name.clone(),
            )
        })
        .collect())
}

fn sort_key(relation: &Staged) -> (&str, u8, &str) {
    (
        relation.database.as_deref().unwrap_or(""),
        u8::from(relation.object_type != "table"),
        &relation.logical_name,
    )
}

fn no_relations(deployment_id: &str) -> String {
    format!("Deployment '{deployment_id}' has no staged model relations to publish")
}

/// `(database, logical name)` bindings to remove, sorted: names an earlier published
/// deployment of a current model bound, that the current publishing mappings no longer bind,
/// and whose recorded relation is still the active binding.
#[must_use]
pub fn obsolete_removals(
    current: &[Mapping],
    deployments: &[Deployment],
    publications: &[(String, Vec<String>)],
    active: &[Binding],
    default_database: &str,
) -> Vec<(String, String)> {
    let binding_name = |mapping: &Mapping| -> (String, String) {
        (
            mapping
                .database
                .as_deref()
                .unwrap_or(default_database)
                .to_owned(),
            mapping.logical_name.clone(),
        )
    };
    let current: Vec<&Mapping> = current.iter().filter(|mapping| mapping.publishes).collect();
    let current_names: HashSet<(String, String)> = current
        .iter()
        .map(|mapping| binding_name(mapping))
        .collect();
    let current_models: HashSet<&str> = current
        .iter()
        .map(|mapping| mapping.logical_model_name.as_str())
        .collect();
    let mut published: HashMap<&str, HashSet<&str>> = HashMap::new();
    for (deployment_id, names) in publications {
        published
            .entry(deployment_id)
            .or_default()
            .extend(names.iter().map(String::as_str));
    }
    let mut historical: Vec<Binding> = Vec::new();
    for deployment in deployments {
        let names: Option<&HashSet<&str>> = published.get(deployment.deployment_id.as_str());
        for mapping in &deployment.mappings {
            let name: (String, String) = binding_name(mapping);
            if current_models.contains(mapping.logical_model_name.as_str())
                && names.is_some_and(|names| names.contains(mapping.logical_name.as_str()))
                && !current_names.contains(&name)
            {
                historical.push((name.0, name.1, mapping.physical_name.clone()));
            }
        }
    }
    historical.sort();
    historical.dedup();
    let active: HashSet<&Binding> = active.iter().collect();
    historical
        .into_iter()
        .filter(|binding| active.contains(binding))
        .map(|(database, logical_name, _)| (database, logical_name))
        .collect()
}

/// The live effects of applying `bindings` and `removals` over the `active` bindings.
#[must_use]
pub fn replacement_preview(
    bindings: &[Binding],
    removals: &[(String, String)],
    active: &[Binding],
) -> Preview {
    let active_by_name: HashMap<(&str, &str), &str> = active
        .iter()
        .map(|(database, logical_name, physical_name)| {
            (
                (database.as_str(), logical_name.as_str()),
                physical_name.as_str(),
            )
        })
        .collect();
    let mut preview: Preview = Preview::default();
    for (position, (database, logical_name, physical_name)) in bindings.iter().enumerate() {
        match active_by_name.get(&(database.as_str(), logical_name.as_str())) {
            None => preview.additions.push(position),
            Some(&current) if current != physical_name => {
                preview.replacements.push((position, current.to_owned()));
            }
            Some(_) => {}
        }
    }
    for (position, (database, logical_name)) in removals.iter().enumerate() {
        if let Some(&current) = active_by_name.get(&(database.as_str(), logical_name.as_str())) {
            preview.removals.push((position, current.to_owned()));
        }
    }
    let mut final_by_name: HashMap<(&str, &str), &str> = active_by_name;
    for (database, logical_name, physical_name) in bindings {
        final_by_name.insert((database, logical_name), physical_name);
    }
    for (database, logical_name) in removals {
        final_by_name.remove(&(database.as_str(), logical_name.as_str()));
    }
    let remaining: HashSet<(&str, &str)> = final_by_name
        .iter()
        .map(|(&(database, _), &physical_name)| (database, physical_name))
        .collect();
    let mut orphaned: Vec<(String, String)> = active
        .iter()
        .map(|(database, _, physical_name)| (database.as_str(), physical_name.as_str()))
        .filter(|relation| !remaining.contains(relation))
        .map(|(database, physical_name)| (database.to_owned(), physical_name.to_owned()))
        .collect();
    orphaned.sort();
    orphaned.dedup();
    preview.orphaned = orphaned;
    preview.initial_publish = !preview.additions.is_empty()
        && preview.additions.len() == bindings.len()
        && removals.is_empty();
    preview
}

#[cfg(test)]
mod tests {
    use super::*;

    fn staged(database: Option<&str>, object_type: &str, logical: &str, physical: &str) -> Staged {
        Staged {
            database: database.map(str::to_owned),
            object_type: object_type.to_owned(),
            logical_name: logical.to_owned(),
            physical_name: physical.to_owned(),
        }
    }

    fn binding(database: &str, logical: &str, physical: &str) -> Binding {
        (database.to_owned(), logical.to_owned(), physical.to_owned())
    }

    fn mapping(database: Option<&str>, logical: &str, physical: &str, model: &str) -> Mapping {
        Mapping {
            database: database.map(str::to_owned),
            publishes: true,
            logical_name: logical.to_owned(),
            physical_name: physical.to_owned(),
            logical_model_name: model.to_owned(),
        }
    }

    #[test]
    fn binds_tables_before_views_with_the_default_database_sorted_first() {
        let relations: Vec<Staged> = vec![
            staged(Some("b"), "table", "orders", "orders__d1"),
            staged(None, "view", "summary", "summary__d1"),
            staged(None, "materialized_view", "orders_mv", "orders_mv__d1"),
            staged(None, "table", "zeta", "zeta__d1"),
        ];
        let existing: HashSet<String> = relations
            .iter()
            .map(|relation| relation.physical_name.clone())
            .collect();
        assert_eq!(
            publish_bindings(&relations, &existing, "main", "d1").unwrap(),
            vec![
                binding("main", "zeta", "zeta__d1"),
                binding("main", "summary", "summary__d1"),
                binding("b", "orders", "orders__d1"),
            ]
        );
    }

    #[test]
    fn missing_relations_are_reported_before_an_empty_publish() {
        let relations: Vec<Staged> = vec![
            staged(None, "materialized_view", "mv", "z__d1"),
            staged(None, "materialized_view", "mv2", "a__d1"),
        ];
        assert_eq!(
            publish_bindings(&relations, &HashSet::new(), "main", "d1").unwrap_err(),
            "Deployment 'd1' is missing staged relations: a__d1, z__d1"
        );
        let existing: HashSet<String> = ["z__d1".to_owned(), "a__d1".to_owned()].into();
        assert_eq!(
            publish_bindings(&relations, &existing, "main", "d1").unwrap_err(),
            "Deployment 'd1' has no staged model relations to publish"
        );
        let candidates: Vec<(String, String)> = vec![
            ("orders__d2".to_owned(), "table".to_owned()),
            ("orders__d1".to_owned(), "materialized_view".to_owned()),
            ("summary__d1".to_owned(), "view".to_owned()),
        ];
        assert_eq!(inspected_candidates(&candidates, "d1").unwrap(), vec![2]);
        assert!(inspected_candidates(&candidates, "d3").is_err());
    }

    #[test]
    fn removes_only_active_renamed_bindings_of_current_models() {
        let mut transform: Mapping = mapping(None, "orders", "orders_mv__d2", "orders");
        transform.publishes = false;
        let current: Vec<Mapping> = vec![
            mapping(None, "orders_v2", "orders_v2__d2", "orders"),
            transform,
        ];
        let deployment = |deployment_id: &str, mappings: Vec<Mapping>| Deployment {
            deployment_id: deployment_id.to_owned(),
            created_at: String::new(),
            incomplete: false,
            mappings,
        };
        let deployments: Vec<Deployment> = vec![
            deployment(
                "d1",
                vec![
                    mapping(None, "orders", "orders__d1", "orders"),
                    mapping(None, "legacy", "legacy__d1", "legacy"),
                    mapping(None, "unpublished", "unpublished__d1", "orders"),
                ],
            ),
            deployment(
                "d0",
                vec![mapping(Some("main"), "orders", "orders__d0", "orders")],
            ),
        ];
        let publications: Vec<(String, Vec<String>)> = vec![
            (
                "d1".to_owned(),
                vec!["orders".to_owned(), "legacy".to_owned()],
            ),
            ("d0".to_owned(), vec!["orders".to_owned()]),
        ];
        let active: Vec<Binding> = vec![
            binding("main", "orders", "orders__d1"),
            binding("main", "legacy", "legacy__d1"),
        ];
        assert_eq!(
            obsolete_removals(&current, &deployments, &publications, &active, "main"),
            vec![("main".to_owned(), "orders".to_owned())]
        );
    }

    #[test]
    fn previews_additions_replacements_removals_and_orphans() {
        let active: Vec<Binding> = vec![
            binding("main", "orders", "orders__d1"),
            binding("main", "legacy", "legacy__d1"),
            binding("main", "same", "same__d1"),
        ];
        let bindings: Vec<Binding> = vec![
            binding("main", "orders", "orders__d2"),
            binding("main", "fresh", "fresh__d2"),
            binding("main", "same", "same__d1"),
        ];
        let removals: Vec<(String, String)> = vec![
            ("main".to_owned(), "legacy".to_owned()),
            ("main".to_owned(), "absent".to_owned()),
        ];
        assert_eq!(
            replacement_preview(&bindings, &removals, &active),
            Preview {
                initial_publish: false,
                additions: vec![1],
                replacements: vec![(0, "orders__d1".to_owned())],
                removals: vec![(0, "legacy__d1".to_owned())],
                orphaned: vec![
                    ("main".to_owned(), "legacy__d1".to_owned()),
                    ("main".to_owned(), "orders__d1".to_owned()),
                ],
            }
        );
        let initial = replacement_preview(&bindings[..2], &[], &[]);
        assert!(initial.initial_publish);
        assert!(!replacement_preview(&[], &[], &[]).initial_publish);
    }
}
