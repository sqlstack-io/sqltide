//! Decide which desired objects exist in the warehouse and where each one's actual spec or
//! query comes from.
//!
//! Keys are interned by the caller; every map here is keyed by those indices.

use std::collections::{HashMap, HashSet};

const RAW_TABLE_NAME_PREFIX: &str = "raw__";
const MATERIALIZED_VIEW_NAME_PREFIX: &str = "mv__";

/// One desired object's kind and the fields actual-state assembly reads.
pub enum DesiredFacts<'a> {
    KafkaTable,
    Table,
    /// A materialized view, with its target table name and desired query.
    MaterializedView {
        target_table_name: &'a str,
    },
    View,
}

/// One desired object.
pub struct DesiredObjectFacts<'a> {
    pub key: usize,
    pub name: &'a str,
    pub kind: DesiredFacts<'a>,
}

/// One managed root's inspected state.
#[derive(Clone, Copy)]
pub struct RootFacts<'a> {
    pub active_view_present: bool,
    pub active_deployment_id: Option<&'a str>,
}

/// One persisted object-state record's relevant fields.
#[derive(Clone, Copy)]
pub struct RecordFacts<'a> {
    pub is_reconcile: bool,
    pub normalized_query: Option<&'a str>,
}

/// Live and persisted inspection inputs, keyed by interned key index.
pub struct InspectionFacts<'a> {
    pub existing_names: HashSet<&'a str>,
    pub roots: HashMap<usize, RootFacts<'a>>,
    pub records_by_deployment_and_key: HashMap<(&'a str, usize), RecordFacts<'a>>,
    pub latest_records: HashMap<usize, RecordFacts<'a>>,
    pub active_physical_names: HashMap<&'a str, &'a str>,
    pub active_table_spec_names: HashSet<&'a str>,
}

/// Where one existing object's actual spec comes from.
#[derive(Debug, PartialEq, Eq)]
pub enum ActualSource<'a> {
    /// The desired spec, unchanged.
    Desired,
    /// The inspected spec of the named active physical table.
    ActiveTable(&'a str),
    /// The desired spec with this query.
    Query(&'a str),
}

/// A lookup the inspection was required to answer but could not.
#[derive(Debug, PartialEq, Eq)]
pub enum ActualStateLookup<'a> {
    /// No root inspection for this key index.
    Root(usize),
    /// No active physical name for this logical name.
    PhysicalName(&'a str),
    /// No inspected spec for this active physical table.
    TableSpec(&'a str),
}

/// For each desired object in order, its actual source, or `None` when it does not exist.
///
/// # Errors
///
/// Returns the first required inspection lookup that is missing, at the point the old
/// per-object assembly would have performed it.
pub fn actual_sources<'a>(
    objects: &[DesiredObjectFacts<'a>],
    inspection: &InspectionFacts<'a>,
) -> Result<Vec<Option<ActualSource<'a>>>, ActualStateLookup<'a>> {
    let model_table_names: HashSet<&str> = objects
        .iter()
        .filter(|object| {
            matches!(object.kind, DesiredFacts::Table)
                && !object.name.starts_with(RAW_TABLE_NAME_PREFIX)
        })
        .map(|object| object.name)
        .collect();
    let mut table_key_by_name: HashMap<&str, usize> = HashMap::new();
    for object in objects.iter().rev() {
        if matches!(object.kind, DesiredFacts::Table) {
            table_key_by_name.insert(object.name, object.key);
        }
    }
    let exists = |name: &str| inspection.existing_names.contains(name);
    let root = |key: usize| {
        inspection
            .roots
            .get(&key)
            .copied()
            .ok_or(ActualStateLookup::Root(key))
    };
    objects
        .iter()
        .map(
            |object| -> Result<Option<ActualSource<'a>>, ActualStateLookup<'a>> {
                Ok(match object.kind {
                    DesiredFacts::KafkaTable => {
                        exists(object.name).then_some(ActualSource::Desired)
                    }
                    DesiredFacts::Table if object.name.starts_with(RAW_TABLE_NAME_PREFIX) => {
                        exists(object.name).then_some(ActualSource::Desired)
                    }
                    DesiredFacts::Table => {
                        if !root(object.key)?.active_view_present {
                            return Ok(None);
                        }
                        let physical: &str = inspection
                            .active_physical_names
                            .get(object.name)
                            .copied()
                            .ok_or(ActualStateLookup::PhysicalName(object.name))?;
                        if !inspection.active_table_spec_names.contains(physical) {
                            return Err(ActualStateLookup::TableSpec(physical));
                        }
                        Some(ActualSource::ActiveTable(physical))
                    }
                    DesiredFacts::MaterializedView { target_table_name } => {
                        let managed: bool = object.name.starts_with(MATERIALIZED_VIEW_NAME_PREFIX);
                        if !managed || !model_table_names.contains(target_table_name) {
                            return Ok(exists(object.name).then_some(ActualSource::Desired));
                        }
                        let root_facts: RootFacts<'a> = root(table_key_by_name[target_table_name])?;
                        if !root_facts.active_view_present {
                            return Ok(None);
                        }
                        let latest_reconcile: Option<&str> = inspection
                            .latest_records
                            .get(&object.key)
                            .filter(|record| record.is_reconcile)
                            .and_then(|record| record.normalized_query);
                        let active: Option<&str> = root_facts.active_deployment_id.and_then(|id| {
                            inspection
                                .records_by_deployment_and_key
                                .get(&(id, object.key))
                                .and_then(|record| record.normalized_query)
                        });
                        Some(
                            latest_reconcile
                                .or(active)
                                .map_or(ActualSource::Desired, ActualSource::Query),
                        )
                    }
                    DesiredFacts::View => inspection
                        .roots
                        .get(&object.key)
                        .filter(|root| root.active_view_present)
                        .and_then(|root| root.active_deployment_id)
                        .and_then(|id| {
                            inspection
                                .records_by_deployment_and_key
                                .get(&(id, object.key))
                        })
                        .and_then(|record| record.normalized_query)
                        .map(ActualSource::Query),
                })
            },
        )
        .collect()
}
