//! Which objects a population creates, in which order, and which extra roots it replays.
//! Keys are indexes into the caller's key list.

use std::collections::{HashMap, HashSet};

/// A key lookup the plan expected to succeed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ScopeError {
    /// No desired object has this key.
    MissingKey(usize),
    /// No desired table has this name.
    MissingName(String),
    /// No root or replay configuration owns a key the plan needs.
    Exhausted,
}

/// Planned keys in creation order: desired-state order, each after its planned dependencies.
/// Dependencies outside the plan are neither created nor traversed.
///
/// # Errors
/// [`ScopeError::MissingKey`] when a planned dependency has no desired object.
pub fn creation_order(
    objects: &[(usize, Vec<usize>)],
    planned: &HashSet<usize>,
) -> Result<Vec<usize>, ScopeError> {
    // The last object with a key wins, as a dict built from the desired objects does.
    let deps_by_key: HashMap<usize, &[usize]> = objects
        .iter()
        .map(|(key, deps)| (*key, deps.as_slice()))
        .collect();
    let mut ordered: Vec<usize> = Vec::new();
    let mut visited: HashSet<usize> = HashSet::new();
    for (key, _) in objects {
        visit(*key, planned, &deps_by_key, &mut ordered, &mut visited)?;
    }
    Ok(ordered)
}

fn visit(
    key: usize,
    planned: &HashSet<usize>,
    deps_by_key: &HashMap<usize, &[usize]>,
    ordered: &mut Vec<usize>,
    visited: &mut HashSet<usize>,
) -> Result<(), ScopeError> {
    if !planned.contains(&key) || !visited.insert(key) {
        return Ok(());
    }
    let deps: &[usize] = deps_by_key.get(&key).ok_or(ScopeError::MissingKey(key))?;
    for dep in deps {
        visit(*dep, planned, deps_by_key, ordered, visited)?;
    }
    ordered.push(key);
    Ok(())
}

/// A materialized view in desired-state order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MaterializedView {
    pub key: usize,
    pub source_table_name: String,
    pub target_table_name: String,
    pub deps: Vec<usize>,
}

/// A catch-up root added for a fan-in target: a copy of root `owner` with these fields.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FanInRoot {
    pub owner: usize,
    pub root_key: usize,
    pub affected_keys: [usize; 2],
    pub upstream_boundary_key: usize,
}

/// Roots that replay each in-scope view's target when a side reference it reads is planned,
/// so direct catch-up does not lose rows the view joined in.
///
/// # Errors
/// [`ScopeError::MissingName`] for a planned view whose target is not a desired table, and
/// [`ScopeError::Exhausted`] when no root affects a target that needs catch-up.
pub fn fan_in_roots(
    planned: &HashSet<usize>,
    roots: &[(usize, Vec<usize>)],
    tables: &[(String, usize)],
    views: &[MaterializedView],
) -> Result<Vec<FanInRoot>, ScopeError> {
    let table_key_by_name: HashMap<&str, usize> = tables
        .iter()
        .map(|(name, key)| (name.as_str(), *key))
        .collect();
    let mut root_keys: HashSet<usize> = roots.iter().map(|(key, _)| *key).collect();
    let mut added: Vec<FanInRoot> = Vec::new();
    for view in views.iter().filter(|view| planned.contains(&view.key)) {
        let target_key: usize = *table_key_by_name
            .get(view.target_table_name.as_str())
            .ok_or_else(|| ScopeError::MissingName(view.target_table_name.clone()))?;
        let Some(&source_key) = table_key_by_name.get(view.source_table_name.as_str()) else {
            continue;
        };
        let reads_planned_side_reference: bool = view
            .deps
            .iter()
            .any(|dep| *dep != source_key && *dep != target_key && planned.contains(dep));
        if root_keys.contains(&target_key) || !reads_planned_side_reference {
            continue;
        }
        let owner: usize = roots
            .iter()
            .position(|(_, affected)| affected.contains(&target_key))
            .ok_or(ScopeError::Exhausted)?;
        added.push(FanInRoot {
            owner,
            root_key: target_key,
            affected_keys: [target_key, view.key],
            upstream_boundary_key: source_key,
        });
        root_keys.insert(target_key);
    }
    Ok(added)
}

/// A desired object's kind and the names source planning reads.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SourceObject {
    Kafka {
        name: String,
    },
    Table {
        name: String,
    },
    MaterializedView {
        name: String,
        target_table_name: String,
    },
    View,
}

/// What source planning does with one desired object.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceAction {
    /// Not part of a managed source.
    Skip,
    /// Already exists and is kept.
    Preserve,
    /// Created now, from its rendered resource.
    Create,
    /// Created after downstream realization, as the source's landing view.
    Land,
}

/// Each object's [`SourceAction`]. Kafka tables, raw landing tables, and views landing into
/// raw tables belong to managed sources.
#[must_use]
pub fn source_actions(
    objects: &[SourceObject],
    raw_prefix: &str,
    existing_names: &HashSet<String>,
) -> Vec<SourceAction> {
    objects
        .iter()
        .map(|object| {
            let (name, landing) = match object {
                SourceObject::Kafka { name } => (name, false),
                SourceObject::Table { name } if name.starts_with(raw_prefix) => (name, false),
                SourceObject::MaterializedView {
                    name,
                    target_table_name,
                } if target_table_name.starts_with(raw_prefix) => (name, true),
                _ => return SourceAction::Skip,
            };
            if existing_names.contains(name) {
                SourceAction::Preserve
            } else if landing {
                SourceAction::Land
            } else {
                SourceAction::Create
            }
        })
        .collect()
}

/// One direct replay root, by model index.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DirectRoot {
    pub model: usize,
    pub propagated_models: Vec<usize>,
    pub driving_input_relation_name: String,
}

/// The kind of one realized model resource.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResourceKind {
    Table,
    MaterializedView,
    View,
    Other,
}

/// A direct closure's population: its planned keys and, per root, its root, affected, and
/// boundary keys.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DirectPopulation {
    pub planned_keys: Vec<usize>,
    pub roots: Vec<(usize, Vec<usize>, usize)>,
}

/// Map a direct closure's models onto the desired keys their realized resources name.
///
/// `desired` holds `(name, key)` of desired tables, materialized views, and views in
/// desired-state order; `resources[model]` holds the model's realized `(kind, name)`s;
/// `external_tables` holds `(table name, key)` of external replay configurations.
///
/// # Errors
/// [`ScopeError::MissingName`] when a realized resource or root table has no desired object,
/// and [`ScopeError::Exhausted`] when a root has no table or its driving input is unknown.
pub fn direct_population(
    desired: &[(String, usize)],
    resources: &[Vec<(ResourceKind, String)>],
    execution_scope: &[usize],
    roots: &[DirectRoot],
    external_tables: &[(String, usize)],
) -> Result<DirectPopulation, ScopeError> {
    let key_by_name: HashMap<&str, usize> = desired
        .iter()
        .map(|(name, key)| (name.as_str(), *key))
        .collect();
    let named_key = |name: &str| {
        key_by_name
            .get(name)
            .copied()
            .ok_or_else(|| ScopeError::MissingName(name.to_owned()))
    };
    let resource_keys = |models: &[usize]| -> Result<Vec<usize>, ScopeError> {
        models
            .iter()
            .flat_map(|model| &resources[*model])
            .filter(|(kind, _)| *kind != ResourceKind::Other)
            .map(|(_, name)| named_key(name))
            .collect()
    };
    let planned_keys: Vec<usize> = resource_keys(execution_scope)?;
    let roots = roots
        .iter()
        .map(|root| {
            let table_name: &str = resources[root.model]
                .iter()
                .find(|(kind, _)| *kind == ResourceKind::Table)
                .map(|(_, name)| name.as_str())
                .ok_or(ScopeError::Exhausted)?;
            let affected: Vec<usize> = resource_keys(&root.propagated_models)?;
            let root_key: usize = named_key(table_name)?;
            let name: &str = &root.driving_input_relation_name;
            let anchor: usize = match key_by_name.get(name) {
                Some(key) => *key,
                None => external_tables
                    .iter()
                    .find(|(table_name, _)| table_name == name)
                    .map(|(_, key)| *key)
                    .ok_or(ScopeError::Exhausted)?,
            };
            Ok((root_key, affected, anchor))
        })
        .collect::<Result<_, ScopeError>>()?;
    Ok(DirectPopulation {
        planned_keys,
        roots,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(keys: &[usize]) -> HashSet<usize> {
        keys.iter().copied().collect()
    }

    #[test]
    fn creation_order_puts_planned_dependencies_first() {
        // 0 <- 1 <- 2, and 3 depends on 2 through unplanned 4.
        let objects = vec![
            (2, vec![1]),
            (3, vec![4]),
            (4, vec![2]),
            (1, vec![0]),
            (0, vec![]),
        ];
        assert_eq!(
            creation_order(&objects, &set(&[0, 1, 2, 3])),
            Ok(vec![0, 1, 2, 3])
        );
        assert_eq!(
            creation_order(&[(0, vec![9])], &set(&[0, 9])),
            Err(ScopeError::MissingKey(9))
        );
    }

    #[test]
    fn fan_in_roots_cover_planned_side_references_once() {
        let tables = vec![
            ("src".to_owned(), 0),
            ("side".to_owned(), 1),
            ("target".to_owned(), 2),
        ];
        let view = |key, deps: Vec<usize>| MaterializedView {
            key,
            source_table_name: "src".to_owned(),
            target_table_name: "target".to_owned(),
            deps,
        };
        let roots = vec![(5, vec![7]), (6, vec![2, 3])];
        let added = fan_in_roots(
            &set(&[1, 3, 4]),
            &roots,
            &tables,
            &[view(3, vec![0, 1, 2]), view(4, vec![0, 1, 2])],
        )
        .unwrap();
        // The second view's target already became a root.
        assert_eq!(
            added,
            [FanInRoot {
                owner: 1,
                root_key: 2,
                affected_keys: [2, 3],
                upstream_boundary_key: 0,
            }]
        );
        // A view reading only its own source and target needs no catch-up.
        assert!(
            fan_in_roots(&set(&[3]), &roots, &tables, &[view(3, vec![0, 2])])
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            fan_in_roots(&set(&[1, 3]), &[], &tables, &[view(3, vec![1])]),
            Err(ScopeError::Exhausted)
        );
    }

    #[test]
    fn source_actions_classify_managed_source_objects() {
        let existing: HashSet<String> = ["raw__kept".to_owned()].into_iter().collect();
        let actions = source_actions(
            &[
                SourceObject::Kafka {
                    name: "kafka__orders".to_owned(),
                },
                SourceObject::Table {
                    name: "raw__kept".to_owned(),
                },
                SourceObject::Table {
                    name: "tbl__model".to_owned(),
                },
                SourceObject::MaterializedView {
                    name: "mv__orders".to_owned(),
                    target_table_name: "raw__orders".to_owned(),
                },
                SourceObject::MaterializedView {
                    name: "mv__model".to_owned(),
                    target_table_name: "tbl__model".to_owned(),
                },
                SourceObject::View,
            ],
            "raw__",
            &existing,
        );
        assert_eq!(
            actions,
            [
                SourceAction::Create,
                SourceAction::Preserve,
                SourceAction::Skip,
                SourceAction::Land,
                SourceAction::Skip,
                SourceAction::Skip,
            ]
        );
    }

    #[test]
    fn direct_population_maps_models_to_desired_keys() {
        let desired = vec![
            ("tbl__a".to_owned(), 0),
            ("mv__a".to_owned(), 1),
            ("tbl__b".to_owned(), 2),
        ];
        let resources = vec![
            vec![
                (ResourceKind::Other, "ignored".to_owned()),
                (ResourceKind::MaterializedView, "mv__a".to_owned()),
                (ResourceKind::Table, "tbl__a".to_owned()),
            ],
            vec![(ResourceKind::Table, "tbl__b".to_owned())],
        ];
        let roots = vec![DirectRoot {
            model: 0,
            propagated_models: vec![0, 1],
            driving_input_relation_name: "external".to_owned(),
        }];
        assert_eq!(
            direct_population(
                &desired,
                &resources,
                &[1, 0],
                &roots,
                &[("external".to_owned(), 9)]
            ),
            Ok(DirectPopulation {
                planned_keys: vec![2, 1, 0],
                roots: vec![(0, vec![1, 0, 2], 9)],
            })
        );
        assert_eq!(
            direct_population(&desired, &resources, &[], &roots, &[]),
            Err(ScopeError::Exhausted)
        );
    }
}
