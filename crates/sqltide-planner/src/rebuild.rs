//! Choose the rebuild subtrees a set of planned object changes requires.
//!
//! Every created or rebuilt object except Kafka tables and stable landing objects (`raw__`
//! tables and the materialized views that fill them) roots a candidate subtree of itself and
//! its downstream objects. Larger subtrees are kept first, and a candidate whose root is
//! already inside a kept subtree is dropped, so each object is rebuilt once.

use std::collections::{HashMap, HashSet};

use crate::desired::{
    CycleError, DesiredGraph, ObjectKind, ObjectType, ReplayMode, ReplayPolicy, ReplayRule,
};

const RAW_TABLE_NAME_PREFIX: &str = "raw__";

/// The planner's classification of one object.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChangeType {
    Create,
    Rebuild,
    Replace,
    NoOp,
}

/// Whether a table's column changes keep existing readers working.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SchemaChangeKind {
    NonBreaking,
    Breaking,
}

/// Whether a changed table can be seeded from the rows it already holds.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SeedCompatibility {
    Seedable,
    NonSeedable,
}

/// How a rebuild subtree is executed.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionMode {
    Full,
    SeededBounded,
    UnseededBounded,
}

impl ExecutionMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Full => "full_rebuild",
            Self::SeededBounded => "seeded_bounded_rebuild",
            Self::UnseededBounded => "unseeded_bounded_rebuild",
        }
    }
}

/// One planned change to one desired object.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObjectChange {
    pub key: usize,
    pub change_type: ChangeType,
    pub force_full_refresh: bool,
    pub forced_start_time: Option<String>,
    pub schema_change_kind: Option<SchemaChangeKind>,
    pub seed_compatibility: Option<SeedCompatibility>,
}

/// One subtree to rebuild together.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RebuildSubtree {
    pub root: usize,
    pub affected: Vec<usize>,
    pub upstream_boundary: usize,
    pub replay_required: bool,
    pub execution_mode: ExecutionMode,
    pub forced_full_refresh: bool,
    pub forced_start_time: Option<String>,
    pub configured_backfill_mode: Option<ReplayMode>,
    pub execution_lookback_seconds: Option<i64>,
}

/// The rebuild subtrees for `changes`, largest first, each root outside every earlier subtree.
///
/// # Errors
///
/// Returns the first [`CycleError`] met while collecting a candidate's closure or boundary,
/// in change order.
pub fn rebuild_subtrees(
    graph: &DesiredGraph,
    changes: &[ObjectChange],
) -> Result<Vec<RebuildSubtree>, CycleError> {
    let mut policy_by_key: HashMap<usize, Option<ReplayPolicy>> = HashMap::new();
    let mut table_or_view_by_key: HashMap<usize, &ObjectKind> = HashMap::new();
    for object in graph.objects() {
        match &object.kind {
            ObjectKind::Table(policy) => {
                policy_by_key.insert(object.key, *policy);
                table_or_view_by_key.insert(object.key, &object.kind);
            }
            kind @ ObjectKind::MaterializedView { .. } => {
                table_or_view_by_key.insert(object.key, kind);
            }
            ObjectKind::KafkaTable | ObjectKind::View => {}
        }
    }
    let is_stable_landing = |key: usize| -> bool {
        let info = graph.key(key);
        match info.object_type {
            ObjectType::Table => info.name.starts_with(RAW_TABLE_NAME_PREFIX),
            ObjectType::MaterializedView => matches!(
                table_or_view_by_key.get(&key),
                Some(ObjectKind::MaterializedView { target, .. })
                    if target.starts_with(RAW_TABLE_NAME_PREFIX)
            ),
            ObjectType::KafkaTable | ObjectType::View => false,
        }
    };
    let mut candidates: Vec<RebuildSubtree> = Vec::new();
    for change in changes {
        if !matches!(change.change_type, ChangeType::Create | ChangeType::Rebuild)
            || graph.key(change.key).object_type == ObjectType::KafkaTable
            || is_stable_landing(change.key)
        {
            continue;
        }
        let policy: Option<ReplayPolicy> = policy_by_key.get(&change.key).copied().flatten();
        candidates.push(candidate(graph, change, policy)?);
    }
    candidates.sort_by(|left, right| {
        let (left_key, right_key) = (graph.key(left.root), graph.key(right.root));
        right
            .affected
            .len()
            .cmp(&left.affected.len())
            .then_with(|| left_key.object_type.cmp(&right_key.object_type))
            .then_with(|| left_key.name.cmp(&right_key.name))
    });
    let mut covered: HashSet<usize> = HashSet::new();
    let mut retained: Vec<RebuildSubtree> = Vec::new();
    for subtree in candidates {
        if covered.contains(&subtree.root) {
            continue;
        }
        covered.extend(subtree.affected.iter().copied());
        retained.push(subtree);
    }
    Ok(retained)
}

fn candidate(
    graph: &DesiredGraph,
    change: &ObjectChange,
    policy: Option<ReplayPolicy>,
) -> Result<RebuildSubtree, CycleError> {
    let (execution_mode, configured_backfill_mode, execution_lookback_seconds) =
        if change.force_full_refresh {
            (ExecutionMode::Full, Some(ReplayMode::Full), None)
        } else if change.forced_start_time.is_some() {
            (ExecutionMode::SeededBounded, None, None)
        } else {
            execution_policy(change, policy)
        };
    let affected: Vec<usize> = graph.descendants(change.key)?;
    let upstream_boundary: usize =
        graph.nearest_replay_anchor(change.key, false, &graph.key(change.key).name)?;
    Ok(RebuildSubtree {
        root: change.key,
        affected,
        upstream_boundary,
        replay_required: graph.key(change.key).object_type != ObjectType::View,
        execution_mode,
        forced_full_refresh: change.force_full_refresh,
        forced_start_time: if change.force_full_refresh {
            None
        } else {
            change.forced_start_time.clone()
        },
        configured_backfill_mode,
        execution_lookback_seconds,
    })
}

fn default_mode(change: &ObjectChange) -> ExecutionMode {
    if change.schema_change_kind == Some(SchemaChangeKind::NonBreaking)
        && change.seed_compatibility == Some(SeedCompatibility::Seedable)
    {
        ExecutionMode::SeededBounded
    } else {
        ExecutionMode::Full
    }
}

fn execution_policy(
    change: &ObjectChange,
    policy: Option<ReplayPolicy>,
) -> (ExecutionMode, Option<ReplayMode>, Option<i64>) {
    let Some(policy) = policy else {
        return (default_mode(change), None, None);
    };
    let rule: Option<ReplayRule> =
        if change.schema_change_kind == Some(SchemaChangeKind::NonBreaking) {
            policy.non_breaking
        } else {
            policy.breaking
        };
    match rule {
        None => (default_mode(change), None, None),
        Some(ReplayRule {
            mode: ReplayMode::Full,
            ..
        }) => (ExecutionMode::Full, Some(ReplayMode::Full), None),
        Some(ReplayRule {
            mode: ReplayMode::Bounded,
            lookback_seconds,
        }) => {
            let mode: ExecutionMode =
                if change.seed_compatibility == Some(SeedCompatibility::Seedable) {
                    ExecutionMode::SeededBounded
                } else {
                    ExecutionMode::UnseededBounded
                };
            (mode, Some(ReplayMode::Bounded), lookback_seconds)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::desired::{DesiredObject, KeyInfo};

    fn change(key: usize, change_type: ChangeType) -> ObjectChange {
        ObjectChange {
            key,
            change_type,
            force_full_refresh: false,
            forced_start_time: None,
            schema_change_kind: None,
            seed_compatibility: None,
        }
    }

    /// raw__orders(0) <- mv(1) [raw target]; orders(2) <- mv_enriched(3) -> enriched(4).
    fn graph(policy: Option<ReplayPolicy>) -> DesiredGraph {
        let key = |object_type, name: &str| KeyInfo {
            object_type,
            name: name.to_owned(),
        };
        DesiredGraph::new(
            vec![
                key(ObjectType::Table, "raw__orders"),
                key(ObjectType::MaterializedView, "mv__raw"),
                key(ObjectType::Table, "orders"),
                key(ObjectType::MaterializedView, "mv__enriched"),
                key(ObjectType::Table, "enriched"),
            ],
            vec![
                DesiredObject {
                    key: 0,
                    deps: vec![],
                    kind: ObjectKind::Table(None),
                },
                DesiredObject {
                    key: 1,
                    deps: vec![0],
                    kind: ObjectKind::MaterializedView {
                        source: 0,
                        target: "raw__orders".to_owned(),
                    },
                },
                DesiredObject {
                    key: 2,
                    deps: vec![0],
                    kind: ObjectKind::Table(None),
                },
                DesiredObject {
                    key: 3,
                    deps: vec![2],
                    kind: ObjectKind::MaterializedView {
                        source: 2,
                        target: "enriched".to_owned(),
                    },
                },
                DesiredObject {
                    key: 4,
                    deps: vec![3],
                    kind: ObjectKind::Table(policy),
                },
            ],
            &[0, 2],
        )
    }

    #[test]
    fn keeps_the_largest_subtree_and_skips_stable_landing_objects() {
        let changes: Vec<ObjectChange> =
            (0..5).map(|key| change(key, ChangeType::Rebuild)).collect();
        let subtrees: Vec<RebuildSubtree> = rebuild_subtrees(&graph(None), &changes).unwrap();
        assert_eq!(subtrees.len(), 1);
        assert_eq!(
            (subtrees[0].root, subtrees[0].affected.clone()),
            (2, vec![2, 3, 4])
        );
        // orders is itself an anchor, but the root may not be its own boundary.
        assert_eq!(subtrees[0].upstream_boundary, 0);
    }

    #[test]
    fn authored_bounded_policy_applies_to_breaking_changes() {
        let policy: ReplayPolicy = ReplayPolicy {
            breaking: Some(ReplayRule {
                mode: ReplayMode::Bounded,
                lookback_seconds: Some(3600),
            }),
            non_breaking: None,
        };
        let mut breaking: ObjectChange = change(4, ChangeType::Rebuild);
        breaking.schema_change_kind = Some(SchemaChangeKind::Breaking);
        breaking.seed_compatibility = Some(SeedCompatibility::NonSeedable);
        let subtree: RebuildSubtree = rebuild_subtrees(&graph(Some(policy)), &[breaking])
            .unwrap()
            .remove(0);
        assert_eq!(
            (
                subtree.execution_mode,
                subtree.configured_backfill_mode,
                subtree.execution_lookback_seconds
            ),
            (
                ExecutionMode::UnseededBounded,
                Some(ReplayMode::Bounded),
                Some(3600)
            )
        );
        let mut seedable: ObjectChange = change(4, ChangeType::Rebuild);
        seedable.schema_change_kind = Some(SchemaChangeKind::NonBreaking);
        seedable.seed_compatibility = Some(SeedCompatibility::Seedable);
        assert_eq!(
            rebuild_subtrees(&graph(Some(policy)), &[seedable]).unwrap()[0].execution_mode,
            ExecutionMode::SeededBounded
        );
    }
}
