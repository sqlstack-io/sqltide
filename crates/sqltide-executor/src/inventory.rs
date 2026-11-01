//! Deployment inventory: each deployment's lifecycle state from its persisted lifecycle record,
//! its publications, and the physical relations and active bindings found in the warehouse.

use std::collections::{BTreeSet, HashMap, HashSet};

use sqltide_planner::naming::{is_deployment_physical_name, split_physical_name};

const INCOMPLETE_STATUS: &str = "incomplete";

/// One persisted deployment record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub deployment_id: String,
    pub created_at: String,
    pub status: String,
    pub selected_root_names: Vec<String>,
    /// `(logical name, physical name)` of each prepared object mapping.
    pub mappings: Vec<(String, String)>,
}

/// Warehouse and metadata evidence for one target database.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Evidence {
    pub records: Vec<Record>,
    pub physical_candidate_names: Vec<String>,
    /// `(logical name, physical name)` of each active binding.
    pub active_bindings: Vec<(String, String)>,
    /// `(deployment ID, published at)` of each publish event.
    pub publications: Vec<(String, String)>,
    pub existing_names: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Active,
    Staged,
    Superseded,
    Incomplete,
    MetadataMissing,
    PhysicalMissing,
}

impl State {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Staged => "staged",
            Self::Superseded => "superseded",
            Self::Incomplete => "incomplete",
            Self::MetadataMissing => "metadata_missing",
            Self::PhysicalMissing => "physical_missing",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    pub deployment_id: String,
    pub state: State,
    pub created_at: Option<String>,
    pub persisted_status: Option<String>,
    pub root_names: Vec<String>,
    pub physical_relation_names: Vec<String>,
    pub missing_physical_relation_names: Vec<String>,
    pub active_binding_names: Vec<String>,
    pub latest_published_at: Option<String>,
}

/// Every deployment named by any evidence, newest deployment ID first. A later record with the
/// same deployment ID replaces an earlier one.
#[must_use]
pub fn deployment_summaries(evidence: &Evidence) -> Vec<Summary> {
    let records: HashMap<&str, &Record> = evidence
        .records
        .iter()
        .map(|record| (record.deployment_id.as_str(), record))
        .collect();
    let mut physical: HashMap<&str, BTreeSet<&str>> = HashMap::new();
    for name in &evidence.physical_candidate_names {
        if let Some(deployment_id) = deployment_id(name) {
            physical.entry(deployment_id).or_default().insert(name);
        }
    }
    let mut active: HashMap<&str, BTreeSet<&str>> = HashMap::new();
    for (logical_name, physical_name) in &evidence.active_bindings {
        if let Some(deployment_id) = deployment_id(physical_name) {
            active
                .entry(deployment_id)
                .or_default()
                .insert(logical_name);
        }
    }
    let mut publications: HashMap<&str, Vec<&str>> = HashMap::new();
    for (deployment_id, published_at) in &evidence.publications {
        publications
            .entry(deployment_id)
            .or_default()
            .push(published_at);
    }
    let existing: HashSet<&str> = evidence.existing_names.iter().map(String::as_str).collect();
    let deployment_ids: BTreeSet<&str> = records
        .keys()
        .chain(physical.keys())
        .chain(active.keys())
        .chain(publications.keys())
        .copied()
        .collect();
    let empty: BTreeSet<&str> = BTreeSet::new();
    deployment_ids
        .into_iter()
        .rev()
        .map(|deployment_id| {
            summary(
                deployment_id,
                records.get(deployment_id).copied(),
                physical.get(deployment_id).unwrap_or(&empty),
                active.get(deployment_id).unwrap_or(&empty),
                publications.get(deployment_id).map_or(&[], Vec::as_slice),
                &existing,
            )
        })
        .collect()
}

fn deployment_id(physical_name: &str) -> Option<&str> {
    if !is_deployment_physical_name(physical_name) {
        return None;
    }
    split_physical_name(physical_name).map(|(_, deployment_id)| deployment_id)
}

fn summary(
    deployment_id: &str,
    record: Option<&Record>,
    discovered: &BTreeSet<&str>,
    active: &BTreeSet<&str>,
    published_at: &[&str],
    existing: &HashSet<&str>,
) -> Summary {
    let mapped: BTreeSet<&str> = record.map_or_else(BTreeSet::new, |record| {
        record
            .mappings
            .iter()
            .map(|(_, physical_name)| physical_name.as_str())
            .collect()
    });
    let missing: Vec<String> = mapped
        .iter()
        .filter(|name| !existing.contains(*name))
        .map(|name| (*name).to_owned())
        .collect();
    let state: State = match record {
        None => State::MetadataMissing,
        Some(record) if record.status == INCOMPLETE_STATUS => State::Incomplete,
        Some(_) if !missing.is_empty() => State::PhysicalMissing,
        Some(_) if !active.is_empty() => State::Active,
        Some(_) if !published_at.is_empty() => State::Superseded,
        Some(_) if mapped.is_empty() => State::PhysicalMissing,
        Some(_) => State::Staged,
    };
    Summary {
        deployment_id: deployment_id.to_owned(),
        state,
        created_at: record.map(|record| record.created_at.clone()),
        persisted_status: record.map(|record| record.status.clone()),
        root_names: record.map_or_else(Vec::new, root_names),
        physical_relation_names: mapped
            .union(discovered)
            .map(|name| (*name).to_owned())
            .collect(),
        missing_physical_relation_names: missing,
        active_binding_names: active.iter().map(|name| (*name).to_owned()).collect(),
        latest_published_at: published_at.iter().max().map(|value| (*value).to_owned()),
    }
}

/// The selected roots, or every mapped logical name when the record names none.
fn root_names(record: &Record) -> Vec<String> {
    let selected: BTreeSet<&str> = record
        .selected_root_names
        .iter()
        .map(String::as_str)
        .collect();
    let names: BTreeSet<&str> = if selected.is_empty() {
        record
            .mappings
            .iter()
            .map(|(logical_name, _)| logical_name.as_str())
            .collect()
    } else {
        selected
    };
    names.into_iter().map(str::to_owned).collect()
}
