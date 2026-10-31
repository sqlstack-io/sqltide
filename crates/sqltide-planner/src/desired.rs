//! Traversal over the realized desired-state object graph.
//!
//! Keys are interned by the caller: every key the graph can name (objects, their dependencies,
//! and each materialized view's source table) has an index, and every result is in indices.
//! An object may appear more than once; the last occurrence wins wherever objects are looked up
//! by key, matching the planner's dictionary semantics.

use std::collections::{BTreeMap, HashMap, HashSet};

/// Desired object types, declared in text order so derived ordering matches it.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum ObjectType {
    KafkaTable,
    MaterializedView,
    Table,
    View,
}

impl ObjectType {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::KafkaTable => "kafka_table",
            Self::MaterializedView => "materialized_view",
            Self::Table => "table",
            Self::View => "view",
        }
    }
}

/// One interned key's sort identity: `(object type, name)`. The database is not part of it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KeyInfo {
    pub object_type: ObjectType,
    pub name: String,
}

/// How a change replays: everything, or a bounded window.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplayMode {
    Full,
    Bounded,
}

impl ReplayMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Bounded => "bounded",
        }
    }
}

/// One authored replay rule: its mode and optional lookback.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReplayRule {
    pub mode: ReplayMode,
    pub lookback_seconds: Option<i64>,
}

/// A table's authored replay policy for breaking and non-breaking schema changes.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ReplayPolicy {
    pub breaking: Option<ReplayRule>,
    pub non_breaking: Option<ReplayRule>,
}

/// What kind of desired object this is, independent of its key's type.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ObjectKind {
    KafkaTable,
    /// A managed table and its replay policy, if authored.
    Table(Option<ReplayPolicy>),
    /// A materialized view, with the key index of `table:<source>` in no database and the
    /// target table name.
    MaterializedView {
        source: usize,
        target: String,
    },
    View,
}

/// One desired object by key index.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DesiredObject {
    pub key: usize,
    pub deps: Vec<usize>,
    pub kind: ObjectKind,
}

/// A dependency cycle among the named keys.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CycleError {
    pub message: String,
}

impl std::fmt::Display for CycleError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for CycleError {}

/// The desired-state graph, built once and queried many times.
#[derive(Clone, Debug)]
pub struct DesiredGraph {
    keys: Vec<KeyInfo>,
    objects: Vec<DesiredObject>,
    /// For each key, the position of its first object, if it is an object.
    first_object: Vec<Option<usize>>,
    /// For each key, the position of its last object, if it is an object.
    last_object: Vec<Option<usize>>,
    /// For each key, downstream object keys sorted by `(type, name)`, one per dependency entry.
    reverse_deps: Vec<Vec<usize>>,
    replay_anchors: HashSet<usize>,
}

impl DesiredGraph {
    /// # Panics
    ///
    /// Panics when an object, dependency, source, or anchor index is not below `keys.len()`.
    #[must_use]
    pub fn new(keys: Vec<KeyInfo>, objects: Vec<DesiredObject>, replay_anchors: &[usize]) -> Self {
        let mut first_object: Vec<Option<usize>> = vec![None; keys.len()];
        let mut last_object: Vec<Option<usize>> = vec![None; keys.len()];
        let mut reverse_deps: Vec<Vec<usize>> = vec![Vec::new(); keys.len()];
        for (position, object) in objects.iter().enumerate() {
            first_object[object.key].get_or_insert(position);
            last_object[object.key] = Some(position);
            for &dep in &object.deps {
                reverse_deps[dep].push(object.key);
            }
        }
        for downstream in &mut reverse_deps {
            downstream.sort_by(|&left, &right| {
                (keys[left].object_type, &keys[left].name)
                    .cmp(&(keys[right].object_type, &keys[right].name))
            });
        }
        Self {
            keys,
            objects,
            first_object,
            last_object,
            reverse_deps,
            replay_anchors: replay_anchors.iter().copied().collect(),
        }
    }

    /// The sort identity of one key.
    #[must_use]
    pub fn key(&self, key: usize) -> &KeyInfo {
        &self.keys[key]
    }

    /// How many keys are interned.
    #[must_use]
    pub fn key_count(&self) -> usize {
        self.keys.len()
    }

    /// Every desired object, in desired order.
    #[must_use]
    pub fn objects(&self) -> &[DesiredObject] {
        &self.objects
    }

    /// Downstream object keys of `key`, sorted by `(type, name)`.
    #[must_use]
    pub fn reverse_deps(&self, key: usize) -> &[usize] {
        &self.reverse_deps[key]
    }

    /// Keys with any downstream object, in first-dependency order.
    #[must_use]
    pub fn reverse_dep_keys(&self) -> Vec<usize> {
        let mut seen: HashSet<usize> = HashSet::new();
        self.objects
            .iter()
            .flat_map(|object| object.deps.iter().copied())
            .filter(|dep| seen.insert(*dep))
            .collect()
    }

    /// The included object keys in dependency order, ties broken by desired position.
    ///
    /// # Errors
    ///
    /// Returns [`CycleError`] naming the unordered keys, in first-object order.
    pub fn order(&self, included: &HashSet<usize>) -> Result<Vec<usize>, CycleError> {
        let mut members: Vec<usize> = included
            .iter()
            .copied()
            .filter(|&key| self.first_object.get(key).copied().flatten().is_some())
            .collect();
        members.sort_unstable_by_key(|&key| self.first_object[key]);
        let first_seen: HashSet<usize> = members.iter().copied().collect();
        let mut indegree: HashMap<usize, i64> = HashMap::with_capacity(members.len());
        for &key in &members {
            let object: &DesiredObject = &self.objects[self.last_object[key].unwrap_or_default()];
            let count: usize = object
                .deps
                .iter()
                .filter(|dep| first_seen.contains(dep))
                .count();
            indegree.insert(key, i64::try_from(count).unwrap_or(i64::MAX));
        }
        let mut ready: BTreeMap<usize, usize> = members
            .iter()
            .filter(|key| indegree[key] == 0)
            .map(|&key| (self.last_object[key].unwrap_or_default(), key))
            .collect();
        let mut ordered: Vec<usize> = Vec::with_capacity(members.len());
        while let Some((_, current)) = ready.pop_first() {
            ordered.push(current);
            for &downstream in &self.reverse_deps[current] {
                let Some(degree) = indegree.get_mut(&downstream) else {
                    continue;
                };
                *degree -= 1;
                if *degree == 0 {
                    ready.insert(self.last_object[downstream].unwrap_or_default(), downstream);
                }
            }
        }
        if ordered.len() == members.len() {
            return Ok(ordered);
        }
        let done: HashSet<usize> = ordered.iter().copied().collect();
        let names: Vec<String> = members
            .iter()
            .filter(|key| !done.contains(key))
            .map(|&key| {
                format!(
                    "{}:{}",
                    self.keys[key].object_type.as_str(),
                    self.keys[key].name
                )
            })
            .collect();
        Err(CycleError {
            message: format!("Dependency cycle detected involving: {}", names.join(", ")),
        })
    }

    /// Whether every key has one object and all objects can be ordered, so no descendant
    /// closure holds a cycle. Duplicate objects count only the last one's dependencies when
    /// ordering, so a closure can then fail while the whole graph orders.
    #[must_use]
    pub fn is_acyclic(&self) -> bool {
        if self.first_object != self.last_object {
            return false;
        }
        let objects: HashSet<usize> = self.objects.iter().map(|object| object.key).collect();
        self.order(&objects).is_ok()
    }

    /// For each key, whether it is a strict descendant of an object in `roots`.
    #[must_use]
    pub fn below(&self, roots: &[usize]) -> Vec<bool> {
        let mut below: Vec<bool> = vec![false; self.keys.len()];
        let mut stack: Vec<usize> = roots
            .iter()
            .filter(|&&root| self.last_object[root].is_some())
            .flat_map(|&root| self.reverse_deps[root].iter().copied())
            .collect();
        while let Some(current) = stack.pop() {
            if !below[current] {
                below[current] = true;
                stack.extend(&self.reverse_deps[current]);
            }
        }
        below
    }

    /// `root` and every object downstream of it, in dependency order; empty when `root` is not
    /// an object.
    ///
    /// # Errors
    ///
    /// Returns [`CycleError`] when the closure holds a cycle.
    pub fn descendants(&self, root: usize) -> Result<Vec<usize>, CycleError> {
        let mut visited: HashSet<usize> = HashSet::new();
        let mut stack: Vec<usize> = vec![root];
        while let Some(current) = stack.pop() {
            if self.last_object[current].is_none() || !visited.insert(current) {
                continue;
            }
            stack.extend(self.reverse_deps[current].iter().rev());
        }
        self.order(&visited)
    }

    /// The nearest replay anchor upstream of `root` through driving dependencies: a
    /// materialized view leads to its source table, any other object to its first table
    /// dependency. Stops at a key that is not an object or has no table dependency.
    ///
    /// # Errors
    ///
    /// Returns [`CycleError`] when the walk revisits a key.
    pub fn nearest_replay_anchor(
        &self,
        root: usize,
        allow_root: bool,
        root_name: &str,
    ) -> Result<usize, CycleError> {
        let mut current: usize = root;
        let mut visited: HashSet<usize> = HashSet::new();
        while visited.insert(current) {
            let Some(position) = self.last_object[current] else {
                return Ok(current);
            };
            let object: &DesiredObject = &self.objects[position];
            if let ObjectKind::MaterializedView { source, .. } = object.kind {
                current = source;
                continue;
            }
            if self.replay_anchors.contains(&current) && (allow_root || current != root) {
                return Ok(current);
            }
            match object
                .deps
                .iter()
                .find(|&&dep| self.keys[dep].object_type == ObjectType::Table)
            {
                Some(&dep) => current = dep,
                None => return Ok(current),
            }
        }
        Err(CycleError {
            message: format!(
                "Dependency cycle detected while finding replay anchor for '{root_name}'"
            ),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(object_type: ObjectType, name: &str) -> KeyInfo {
        KeyInfo {
            object_type,
            name: name.to_owned(),
        }
    }

    fn object(key: usize, deps: &[usize], source: Option<usize>) -> DesiredObject {
        DesiredObject {
            key,
            deps: deps.to_vec(),
            kind: source.map_or(ObjectKind::Table(None), |source| {
                ObjectKind::MaterializedView {
                    source,
                    target: "enriched".to_owned(),
                }
            }),
        }
    }

    /// raw(0) <- mv(1) -> enriched(2) <- view(3); enriched also references dim(4).
    fn graph() -> DesiredGraph {
        DesiredGraph::new(
            vec![
                key(ObjectType::Table, "raw"),
                key(ObjectType::MaterializedView, "mv"),
                key(ObjectType::Table, "enriched"),
                key(ObjectType::View, "report"),
                key(ObjectType::Table, "dim"),
            ],
            vec![
                object(3, &[2], None),
                object(2, &[1, 4], None),
                object(1, &[0], Some(0)),
                object(0, &[], None),
                object(4, &[], None),
            ],
            &[0],
        )
    }

    #[test]
    fn orders_upstreams_first_with_desired_position_breaking_ties() {
        let graph: DesiredGraph = graph();
        let all: HashSet<usize> = (0..5).collect();
        // raw (position 3) and dim (position 4) are both ready; raw comes first by position.
        assert_eq!(graph.order(&all), Ok(vec![0, 1, 4, 2, 3]));
        assert_eq!(graph.descendants(1), Ok(vec![1, 2, 3]));
        assert_eq!(graph.descendants(4), Ok(vec![4, 2, 3]));
    }

    #[test]
    fn replay_anchor_walk_skips_the_root_unless_allowed() {
        let graph: DesiredGraph = graph();
        assert_eq!(graph.nearest_replay_anchor(0, true, "raw"), Ok(0));
        // Not allowed to stop at raw itself, and raw has no table dependency: it is returned.
        assert_eq!(graph.nearest_replay_anchor(0, false, "raw"), Ok(0));
        // enriched -> its first table dependency is dim (mv is not a table), which has no deps.
        assert_eq!(graph.nearest_replay_anchor(2, false, "enriched"), Ok(4));
        assert_eq!(graph.nearest_replay_anchor(1, false, "mv"), Ok(0));
    }

    #[test]
    fn names_every_key_left_on_a_cycle() {
        let graph: DesiredGraph = DesiredGraph::new(
            vec![key(ObjectType::Table, "b"), key(ObjectType::Table, "a")],
            vec![object(0, &[1], None), object(1, &[0], None)],
            &[],
        );
        assert_eq!(
            graph.order(&(0..2).collect()).unwrap_err().message,
            "Dependency cycle detected involving: table:b, table:a"
        );
        assert!(graph.nearest_replay_anchor(0, true, "b").is_err());
    }
}
