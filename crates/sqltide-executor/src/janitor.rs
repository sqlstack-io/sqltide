//! Which retained deployments the janitor may drop, and which stale bindings it removes.

use std::collections::{HashMap, HashSet};

use sqltide_planner::naming::{is_deployment_physical_name, split_physical_name};

use crate::publications::{Binding, Rank, identity};

/// One prepared object of a deployment.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Mapping {
    pub database: Option<String>,
    /// Whether publishing binds this object: tables and views.
    pub publishes: bool,
    pub logical_name: String,
    pub physical_name: String,
    pub logical_model_name: String,
}

/// One recorded deployment.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Deployment {
    pub deployment_id: String,
    pub created_at: String,
    pub incomplete: bool,
    pub mappings: Vec<Mapping>,
}

/// One recorded publication, with every time parsed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Publication {
    pub deployment_id: String,
    pub rank: Rank,
    /// The publication time read as UTC whatever offset it records, in microseconds.
    pub published_at: i64,
    pub logical_view_names: Vec<String>,
    pub bindings: Vec<Binding>,
}

/// The retention policy one janitor run applies.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Retention {
    pub database: String,
    pub retention_days: i64,
    /// Publications at or after this time, in UTC microseconds, are retained.
    pub cutoff: i64,
    pub minimum_rollback_deployments: usize,
}

/// The verdict for one deployment.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Decision {
    pub deployment: usize,
    pub deletable: bool,
    pub reason: String,
}

/// Active bindings to stable views: the physical names they protect, and the
/// `(database, logical name)` bindings to remove because a later publication of the same
/// model superseded them.
#[must_use]
pub fn binding_activity(
    deployments: &[Deployment],
    publications: &[Publication],
    active: &[Binding],
) -> (HashSet<String>, Vec<(String, String)>) {
    let ranks: HashMap<&str, &Rank> = latest_ranks(publications);
    let mut published_names: HashMap<&str, HashSet<&str>> = HashMap::new();
    for publication in publications {
        published_names
            .entry(publication.deployment_id.as_str())
            .or_default()
            .extend(publication.logical_view_names.iter().map(String::as_str));
    }
    let mut known: HashSet<&str> = HashSet::new();
    let mut latest_by_model: HashMap<&str, (&Rank, &str)> = HashMap::new();
    for deployment in deployments {
        let Some(&rank) = ranks.get(deployment.deployment_id.as_str()) else {
            continue;
        };
        let names: Option<&HashSet<&str>> = published_names.get(deployment.deployment_id.as_str());
        for mapping in &deployment.mappings {
            if !names.is_some_and(|names| names.contains(mapping.logical_name.as_str())) {
                continue;
            }
            known.insert(&mapping.physical_name);
            let candidate: (&Rank, &str) = (rank, &mapping.physical_name);
            latest_by_model
                .entry(&mapping.logical_model_name)
                .and_modify(|current| {
                    if candidate > *current {
                        *current = candidate;
                    }
                })
                .or_insert(candidate);
        }
    }
    let latest: HashSet<&str> = latest_by_model.values().map(|(_, name)| *name).collect();
    let mut protected: HashSet<String> = HashSet::new();
    let mut removals: Vec<(String, String)> = Vec::new();
    for (database, logical_name, physical_name) in active {
        if known.contains(physical_name.as_str()) && !latest.contains(physical_name.as_str()) {
            removals.push((database.clone(), logical_name.clone()));
        } else {
            protected.insert(physical_name.clone());
        }
    }
    (protected, removals)
}

/// A verdict for every deployment, newest created first. `physical_relations` holds the
/// `(database, name)` relations that exist, and is only read when rollback points are kept.
#[must_use]
pub fn preview(
    deployments: &[Deployment],
    publications: &[Publication],
    active: &[Binding],
    physical_relations: &HashSet<(String, String)>,
    retention: &Retention,
) -> Vec<Decision> {
    let mut published_at: HashMap<&str, i64> = HashMap::new();
    for publication in publications {
        published_at
            .entry(&publication.deployment_id)
            .and_modify(|latest| *latest = (*latest).max(publication.published_at))
            .or_insert(publication.published_at);
    }
    let (protected, _) = binding_activity(deployments, publications, active);
    let active_deployments: HashSet<&str> = active
        .iter()
        .map(|(_, _, physical_name)| physical_name.as_str())
        .filter(|name| protected.contains(*name) && is_deployment_physical_name(name))
        .filter_map(|name| split_physical_name(name).map(|(_, deployment_id)| deployment_id))
        .collect();
    let rollback_points: HashSet<&str> = rollback_points(
        deployments,
        publications,
        &active_deployments,
        physical_relations,
        retention,
    );
    let mut order: Vec<usize> = (0..deployments.len()).collect();
    order.sort_by(|&left, &right| {
        deployments[right]
            .created_at
            .cmp(&deployments[left].created_at)
    });
    order
        .into_iter()
        .map(|position| {
            let deployment: &Deployment = &deployments[position];
            let id: &str = &deployment.deployment_id;
            let kept = |reason: String| Decision {
                deployment: position,
                deletable: false,
                reason,
            };
            if !mappings_are_safe(deployment) {
                return kept("physical mappings do not match deployment identity".to_owned());
            }
            if deployment
                .mappings
                .iter()
                .any(|mapping| protected.contains(&mapping.physical_name))
            {
                return kept("contains currently active relation".to_owned());
            }
            if active_deployments.contains(id) {
                return kept("currently active".to_owned());
            }
            if rollback_points.contains(id) {
                return kept(format!(
                    "retained as rollback point (minimum {} deployments)",
                    retention.minimum_rollback_deployments
                ));
            }
            let days: i64 = retention.retention_days;
            match published_at.get(id) {
                Some(&time) if time >= retention.cutoff => {
                    kept(format!("published within retention window ({days} days)"))
                }
                published => Decision {
                    deployment: position,
                    deletable: true,
                    reason: if published.is_some() {
                        format!("published before retention window ({days} days)")
                    } else {
                        "stale unpublished deployment".to_owned()
                    },
                },
            }
        })
        .collect()
}

/// The newest restorable published deployments, up to the retention minimum.
fn rollback_points<'a>(
    deployments: &'a [Deployment],
    publications: &[Publication],
    active_deployments: &HashSet<&str>,
    physical_relations: &HashSet<(String, String)>,
    retention: &Retention,
) -> HashSet<&'a str> {
    if retention.minimum_rollback_deployments == 0 {
        return HashSet::new();
    }
    let ranks: HashMap<&str, &Rank> = latest_ranks(publications);
    let database = |mapping: &Mapping| -> String {
        mapping
            .database
            .clone()
            .unwrap_or_else(|| retention.database.clone())
    };
    let mut eligible: Vec<(&Rank, &str)> = deployments
        .iter()
        .filter(|deployment| {
            let id: &str = &deployment.deployment_id;
            let expected: Vec<Binding> = identity(
                &deployment
                    .mappings
                    .iter()
                    .filter(|mapping| mapping.publishes)
                    .map(|mapping| {
                        (
                            database(mapping),
                            mapping.logical_name.clone(),
                            mapping.physical_name.clone(),
                        )
                    })
                    .collect::<Vec<Binding>>(),
            );
            ranks.contains_key(id)
                && !active_deployments.contains(id)
                && !deployment.incomplete
                && mappings_are_safe(deployment)
                && !deployment.mappings.is_empty()
                && deployment.mappings.iter().all(|mapping| {
                    physical_relations.contains(&(database(mapping), mapping.physical_name.clone()))
                })
                && !expected.is_empty()
                && publications.iter().any(|publication| {
                    publication.deployment_id == id && identity(&publication.bindings) == expected
                })
        })
        .map(|deployment| {
            (
                ranks[deployment.deployment_id.as_str()],
                deployment.deployment_id.as_str(),
            )
        })
        .collect();
    eligible.sort_by(|left, right| right.0.cmp(left.0));
    eligible
        .into_iter()
        .take(retention.minimum_rollback_deployments)
        .map(|(_, id)| id)
        .collect()
}

fn latest_ranks(publications: &[Publication]) -> HashMap<&str, &Rank> {
    let mut ranks: HashMap<&str, &Rank> = HashMap::new();
    for publication in publications {
        ranks
            .entry(&publication.deployment_id)
            .and_modify(|latest| {
                if publication.rank > **latest {
                    *latest = &publication.rank;
                }
            })
            .or_insert(&publication.rank);
    }
    ranks
}

/// Whether every physical name is the mapping's logical name suffixed with this deployment.
fn mappings_are_safe(deployment: &Deployment) -> bool {
    deployment.mappings.iter().all(|mapping| {
        is_deployment_physical_name(&mapping.physical_name)
            && split_physical_name(&mapping.physical_name)
                == Some((&mapping.logical_name, &deployment.deployment_id))
    })
}
