//! Managed-root inspection: which catalog relations are deployment candidates or active bindings,
//! and how one managed root presents itself in the warehouse.

use std::collections::HashMap;
use std::collections::hash_map::Entry;

use crate::naming::{is_deployment_physical_name, split_physical_name};

const KAFKA_TABLE_NAME_PREFIX: &str = "kafka__";
const MATERIALIZED_VIEW_NAME_PREFIX: &str = "mv__";

/// One catalog relation's name, whether it is a view, and the physical name it binds to.
pub struct Relation<'a> {
    pub name: &'a str,
    pub is_view: bool,
    pub stable_binding_name: Option<&'a str>,
}

/// `(logical name, physical name, is view)` of one deployment-suffixed physical candidate.
pub type Candidate<'a> = (&'a str, &'a str, bool);

/// `(logical name, physical name)` of one stable binding.
pub type Binding<'a> = (&'a str, &'a str);

/// Split catalog relations into deployment-suffixed candidates, excluding Kafka tables and
/// materialized views, and the remaining relations that bind a stable name.
#[must_use]
pub fn managed_relations<'a>(relations: &[Relation<'a>]) -> (Vec<Binding<'a>>, Vec<Candidate<'a>>) {
    let mut bindings: Vec<Binding<'a>> = Vec::new();
    let mut candidates: Vec<Candidate<'a>> = Vec::new();
    for relation in relations {
        if let Some(logical_name) = deployment_candidate_logical_name(relation.name) {
            candidates.push((logical_name, relation.name, relation.is_view));
            continue;
        }
        if let Some(physical_name) = relation.stable_binding_name {
            bindings.push((relation.name, physical_name));
        }
    }
    (bindings, candidates)
}

/// The logical name of a deployment-suffixed model relation, excluding Kafka tables and
/// materialized views.
#[must_use]
pub fn deployment_candidate_logical_name(physical_name: &str) -> Option<&str> {
    if !is_deployment_physical_name(physical_name) {
        return None;
    }
    split_physical_name(physical_name)
        .map(|(logical_name, _)| logical_name)
        .filter(|logical_name| {
            !logical_name.starts_with(KAFKA_TABLE_NAME_PREFIX)
                && !logical_name.starts_with(MATERIALIZED_VIEW_NAME_PREFIX)
        })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RootState<'a> {
    ActiveViewPresent(&'a str),
    Greenfield,
    LogicalViewMissing,
    InvalidActiveView,
}

impl RootState<'_> {
    #[must_use]
    pub const fn kind(self) -> &'static str {
        match self {
            Self::ActiveViewPresent(_) => "active_view_present",
            Self::Greenfield => "greenfield",
            Self::LogicalViewMissing => "logical_view_missing",
            Self::InvalidActiveView => "invalid_active_view",
        }
    }
}

/// `(database, logical name, physical name)` of one inspected binding or candidate.
pub type Inspected<'a> = (&'a str, &'a str, &'a str);

/// One root's state from the bindings and candidates with its name, in its database when it
/// names a non-empty one: exactly one binding decides it, otherwise any candidate means its
/// logical view is missing.
#[must_use]
pub fn root_state<'a>(
    bindings: &[Inspected<'a>],
    candidates: &[Inspected<'a>],
    root_database: Option<&str>,
    root_name: &str,
) -> RootState<'a> {
    let root_database: Option<&str> = root_database.filter(|database| !database.is_empty());
    let matches = |(database, logical_name, _): &&Inspected<'a>| {
        *logical_name == root_name && root_database.is_none_or(|root| root == *database)
    };
    let physical_names: Vec<&'a str> = bindings
        .iter()
        .filter(matches)
        .map(|(_, _, physical_name)| *physical_name)
        .collect();
    match physical_names.as_slice() {
        [physical_name] if is_deployment_physical_name(physical_name) => {
            RootState::ActiveViewPresent(
                split_physical_name(physical_name).map_or("", |(_, deployment_id)| deployment_id),
            )
        }
        [_] => RootState::InvalidActiveView,
        _ if candidates.iter().any(|candidate| matches(&candidate)) => {
            RootState::LogicalViewMissing
        }
        _ => RootState::Greenfield,
    }
}

/// `(key index when the key is wanted, recorded at, observation ID)` of one object-state record.
pub type RecordStamp<'a> = (Option<usize>, &'a str, &'a str);

/// The position of each wanted key's latest record, ordered by each key's first record: the
/// greatest `(recorded at, observation ID)`, keeping the earlier record on a tie.
#[must_use]
pub fn latest_record_positions(records: &[RecordStamp<'_>]) -> Vec<usize> {
    let mut order: Vec<usize> = Vec::new();
    let mut latest: HashMap<usize, usize> = HashMap::new();
    for (position, (key, recorded_at, observation_id)) in records.iter().enumerate() {
        let Some(key) = key else {
            continue;
        };
        match latest.entry(*key) {
            Entry::Vacant(entry) => {
                entry.insert(position);
                order.push(*key);
            }
            Entry::Occupied(mut entry) => {
                let (_, current_recorded_at, current_observation_id) = records[*entry.get()];
                if (*recorded_at, *observation_id) > (current_recorded_at, current_observation_id) {
                    entry.insert(position);
                }
            }
        }
    }
    order.iter().map(|key| latest[key]).collect()
}
