//! Assemble the actual state of the desired objects from one captured warehouse snapshot:
//! inspect each managed root, index the persisted object-state records, check the active
//! tables' catalog metadata, then decide each object's actual source.

use std::collections::{BTreeSet, HashMap, HashSet};

use crate::actual::{
    ActualSource, ActualStateLookup, DesiredFacts, DesiredObjectFacts, InspectionFacts,
    RecordFacts, RootFacts, actual_sources,
};
use crate::inspection::{
    Inspected, Relation, RootState, latest_record_positions, managed_relations, root_state,
};

const RAW_TABLE_NAME_PREFIX: &str = "raw__";
const ENGINE_ARGUMENT_OPEN: char = '(';

/// One object identity, equal on all three parts as the Python `ObjectKey` is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ObjectKeyRef<'a> {
    pub database: Option<&'a str>,
    pub object_type: &'a str,
    pub name: &'a str,
}

/// One desired object: its key, name and kind.
pub struct DesiredObject<'a> {
    pub key: ObjectKeyRef<'a>,
    pub name: &'a str,
    pub kind: DesiredFacts<'a>,
}

/// One persisted object-state record's fields that assembly reads.
pub struct StateRecord<'a> {
    pub deployment_id: &'a str,
    pub key: ObjectKeyRef<'a>,
    pub is_reconcile: bool,
    pub normalized_query: Option<&'a str>,
    pub recorded_at: &'a str,
    pub observation_id: &'a str,
}

/// Why assembly stopped, in the terms the Python assembly raised.
#[derive(Debug, PartialEq, Eq)]
pub enum AssemblyError<'a> {
    /// A required lookup by this object key was missing (`KeyError(key)`).
    MissingKey(ObjectKeyRef<'a>),
    /// A required lookup by this name was missing (`KeyError(name)`).
    MissingName(&'a str),
    /// An active physical table has no catalog relation (`ActualStateError`).
    MissingTableMetadata(&'a str),
}

fn is_python_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// A catalog storage engine as a comparable table engine: stripped, with `()` when it has no
/// argument list.
#[must_use]
pub fn normalize_storage_engine(engine: &str) -> String {
    let normalized: &str = engine.trim_matches(is_python_space);
    if normalized.contains(ENGINE_ARGUMENT_OPEN) {
        normalized.to_owned()
    } else {
        format!("{normalized}()")
    }
}

fn is_model_table(object: &DesiredObject<'_>) -> bool {
    matches!(object.kind, DesiredFacts::Table) && !object.name.starts_with(RAW_TABLE_NAME_PREFIX)
}

/// Each existing desired object's position and actual source, ordered by object type then name
/// and otherwise in desired order.
///
/// `relations` are the catalog relations of `catalog_database`, whose names a stable binding
/// may name as its active physical table.
///
/// # Errors
///
/// Returns the first lookup the Python assembly would have failed, at the point it failed.
pub fn assemble_actual_sources<'a>(
    objects: &[DesiredObject<'a>],
    catalog_database: &'a str,
    relations: &[Relation<'a>],
    records: &[StateRecord<'a>],
) -> Result<Vec<(usize, ActualSource<'a>)>, AssemblyError<'a>> {
    let mut keys: Vec<ObjectKeyRef<'a>> = Vec::new();
    let mut index_by_key: HashMap<ObjectKeyRef<'a>, usize> = HashMap::new();
    for object in objects {
        index_by_key.entry(object.key).or_insert_with(|| {
            keys.push(object.key);
            keys.len() - 1
        });
    }
    let (bindings, candidates) = managed_relations(relations);
    let inspected_bindings: Vec<Inspected<'a>> = bindings
        .iter()
        .map(|(logical, physical)| (catalog_database, *logical, *physical))
        .collect();
    let inspected_candidates: Vec<Inspected<'a>> = candidates
        .iter()
        .map(|(logical, physical, _)| (catalog_database, *logical, *physical))
        .collect();
    let mut roots: HashMap<usize, RootFacts<'a>> = HashMap::new();
    for object in objects {
        if !is_model_table(object) && !matches!(object.kind, DesiredFacts::View) {
            continue;
        }
        let state: RootState<'a> = root_state(
            &inspected_bindings,
            &inspected_candidates,
            object.key.database,
            object.key.name,
        );
        let active_deployment_id: Option<&'a str> = match state {
            RootState::ActiveViewPresent(deployment_id) => Some(deployment_id),
            _ => None,
        };
        roots.insert(
            index_by_key[&object.key],
            RootFacts {
                active_view_present: active_deployment_id.is_some(),
                active_deployment_id,
            },
        );
    }
    let active_deployment_ids: HashSet<&'a str> = roots
        .values()
        .filter_map(|root| root.active_deployment_id)
        .collect();
    let facts = |record: &StateRecord<'a>| RecordFacts {
        is_reconcile: record.is_reconcile,
        normalized_query: record.normalized_query,
    };
    let mut records_by_deployment_and_key: HashMap<(&'a str, usize), RecordFacts<'a>> =
        HashMap::new();
    for record in records {
        if !active_deployment_ids.contains(record.deployment_id) {
            continue;
        }
        if let Some(&key) = index_by_key.get(&record.key) {
            records_by_deployment_and_key.insert((record.deployment_id, key), facts(record));
        }
    }
    let model_table_names: HashSet<&str> = objects
        .iter()
        .filter(|object| is_model_table(object))
        .map(|object| object.name)
        .collect();
    let transform_view_keys: HashSet<usize> = objects
        .iter()
        .filter(|object| {
            matches!(object.kind, DesiredFacts::MaterializedView { target_table_name }
                if model_table_names.contains(target_table_name))
        })
        .map(|object| index_by_key[&object.key])
        .collect();
    let stamps: Vec<(Option<usize>, &str, &str)> = records
        .iter()
        .map(|record| {
            (
                index_by_key
                    .get(&record.key)
                    .copied()
                    .filter(|key| transform_view_keys.contains(key)),
                record.recorded_at,
                record.observation_id,
            )
        })
        .collect();
    let latest_records: HashMap<usize, RecordFacts<'a>> = latest_record_positions(&stamps)
        .into_iter()
        .map(|position| {
            (
                index_by_key[&records[position].key],
                facts(&records[position]),
            )
        })
        .collect();
    let mut active_physical_names: HashMap<&'a str, &'a str> = HashMap::new();
    for (logical, physical) in &bindings {
        active_physical_names.insert(logical, physical);
    }
    let mut active_table_names: BTreeSet<&'a str> = BTreeSet::new();
    for object in objects {
        if !is_model_table(object) || !roots[&index_by_key[&object.key]].active_view_present {
            continue;
        }
        let physical: &'a str = active_physical_names
            .get(object.name)
            .copied()
            .ok_or(AssemblyError::MissingName(object.name))?;
        active_table_names.insert(physical);
    }
    let existing_names: HashSet<&'a str> = relations.iter().map(|relation| relation.name).collect();
    if let Some(missing) = active_table_names
        .iter()
        .find(|name| !existing_names.contains(**name))
    {
        return Err(AssemblyError::MissingTableMetadata(missing));
    }
    let desired: Vec<DesiredObjectFacts<'a>> = objects
        .iter()
        .map(|object| DesiredObjectFacts {
            key: index_by_key[&object.key],
            name: object.name,
            kind: match object.kind {
                DesiredFacts::KafkaTable => DesiredFacts::KafkaTable,
                DesiredFacts::Table => DesiredFacts::Table,
                DesiredFacts::MaterializedView { target_table_name } => {
                    DesiredFacts::MaterializedView { target_table_name }
                }
                DesiredFacts::View => DesiredFacts::View,
            },
        })
        .collect();
    let inspection = InspectionFacts {
        existing_names,
        roots,
        records_by_deployment_and_key,
        latest_records,
        active_physical_names,
        active_table_spec_names: active_table_names.into_iter().collect(),
    };
    let sources = actual_sources(&desired, &inspection).map_err(|lookup| match lookup {
        ActualStateLookup::Root(key) => AssemblyError::MissingKey(keys[key]),
        ActualStateLookup::PhysicalName(name) | ActualStateLookup::TableSpec(name) => {
            AssemblyError::MissingName(name)
        }
    })?;
    let mut assembled: Vec<(usize, ActualSource<'a>)> = sources
        .into_iter()
        .enumerate()
        .filter_map(|(position, source)| source.map(|source| (position, source)))
        .collect();
    assembled.sort_by_key(|(position, _)| {
        (
            objects[*position].key.object_type,
            objects[*position].key.name,
        )
    });
    Ok(assembled)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(object_type: &'static str, name: &'static str) -> ObjectKeyRef<'static> {
        ObjectKeyRef {
            database: None,
            object_type,
            name,
        }
    }

    fn relation(
        name: &'static str,
        stable_binding_name: Option<&'static str>,
    ) -> Relation<'static> {
        Relation {
            name,
            is_view: stable_binding_name.is_some(),
            stable_binding_name,
        }
    }

    #[test]
    fn engine_gains_an_empty_argument_list_only_without_one() {
        assert_eq!(normalize_storage_engine(" MergeTree\u{1c}"), "MergeTree()");
        assert_eq!(
            normalize_storage_engine("ReplacingMergeTree(v) "),
            "ReplacingMergeTree(v)"
        );
    }

    #[test]
    fn active_table_without_catalog_metadata_stops_assembly() {
        let objects = [DesiredObject {
            key: key("table", "orders"),
            name: "orders",
            kind: DesiredFacts::Table,
        }];
        let relations = [relation("orders", Some("orders__20260131T120000Z_ab12"))];
        assert_eq!(
            assemble_actual_sources(&objects, "analytics", &relations, &[]),
            Err(AssemblyError::MissingTableMetadata(
                "orders__20260131T120000Z_ab12"
            ))
        );
    }
}
