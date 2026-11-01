//! Direct-mode plans: the execution closure, preserved prerequisites, replay roots, and the
//! relation operations that rebuild a selected downstream closure.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;

use sha2::{Digest, Sha256};

use crate::unified_diff::unified_diff;

const PARTITION_COLUMN: &str = "_replay_partition";
const OFFSET_COLUMN: &str = "_replay_offset";
const TIMESTAMP_COLUMN: &str = "_replay_timestamp";
const LANDED_AT_COLUMN: &str = "_replay_landed_at";
const CURSOR_COLUMN: &str = "_replay_cursor";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SelectionMode {
    AllModels,
    Explicit,
    Changed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResourceKind {
    Table,
    MaterializedView,
    View,
}

impl ResourceKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Table => "table",
            Self::MaterializedView => "materialized_view",
            Self::View => "view",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplayLineage {
    Offsets,
    Timestamp,
    LandedAt,
    Cursor,
}

impl ReplayLineage {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Offsets => "offsets",
            Self::Timestamp => "timestamp",
            Self::LandedAt => "landed_at",
            Self::Cursor => "cursor",
        }
    }
}

/// What a graph key is.
pub enum NodeKind<'a> {
    Source {
        /// Whether the framework creates the source, so a missing relation is not fatal.
        framework_managed: bool,
    },
    Model {
        /// A table model replays from a driving input; other models are views.
        table: bool,
        aggregate: bool,
        query: &'a str,
        replay_settings: Vec<(&'a str, &'a str)>,
    },
}

/// One logical graph key in topological order.
pub struct Node<'a> {
    pub name: &'a str,
    pub kind: NodeKind<'a>,
    pub relation_name: &'a str,
    /// Realized adapter resources; `None` marks a kind direct mode cannot build.
    pub resources: Vec<(&'a str, Option<ResourceKind>)>,
    pub replay_lineage: Option<ReplayLineage>,
    /// Whether a scheduled postgres source shares this key's name.
    pub postgres_source_name: bool,
    pub producing_pipeline: Option<&'a str>,
    /// `(upstream key, is driving input)` in edge order.
    pub upstream: Vec<(usize, bool)>,
    pub downstream: Vec<usize>,
}

/// Replay column overrides from an adopted external source.
#[derive(Default)]
pub struct ExternalColumns<'a> {
    pub partition: Option<&'a str>,
    pub offset: Option<&'a str>,
    pub timestamp: Option<&'a str>,
    pub landed_at: Option<&'a str>,
    pub cursor: Option<&'a str>,
}

/// One observed warehouse relation.
pub struct CatalogRelation<'a> {
    pub name: &'a str,
    pub materialized_view: bool,
    pub columns: Vec<&'a str>,
    pub source_relation_names: Vec<&'a str>,
}

/// Applied query baselines for change reporting.
pub enum Fingerprints<'a> {
    Unavailable {
        warning: Option<&'a str>,
    },
    /// `(logical model identity, definition SQL, definition hash)`.
    Available(Vec<(&'a str, &'a str, &'a str)>),
}

pub struct DirectInput<'a> {
    pub nodes: Vec<Node<'a>>,
    pub database: &'a str,
    pub selection_mode: SelectionMode,
    pub selected: HashSet<usize>,
    pub source_drift: HashSet<usize>,
    pub include_missing_upstream: bool,
    pub has_start_time: bool,
    pub catalog: Vec<CatalogRelation<'a>>,
    /// By adopted relation name, in configuration order.
    pub external_columns: Vec<(&'a str, ExternalColumns<'a>)>,
    pub fingerprints: Fingerprints<'a>,
    /// Names of keys that read mutable side refs.
    pub mutable_ref_names: Vec<&'a str>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Prerequisite {
    pub key: usize,
    pub relation_name: String,
    pub present: bool,
    pub framework_managed: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub struct SqlChange {
    pub status: &'static str,
    pub current_hash: String,
    /// `(previous SQL, previous hash)`.
    pub previous: Option<(String, String)>,
    pub unified_diff: Option<String>,
    pub warning: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Entry {
    pub key: usize,
    pub reason: &'static str,
    pub relations: Vec<(String, ResourceKind)>,
    pub driving_input: Option<usize>,
    pub is_replay_root: bool,
    pub sql_change: SqlChange,
}

#[derive(Debug, PartialEq, Eq)]
pub struct ReplayColumns {
    pub partition: String,
    pub offset: String,
    pub timestamp: String,
    pub landed_at: String,
    pub cursor: String,
}

#[derive(Debug, PartialEq, Eq)]
pub struct ReplayRoot {
    pub key: usize,
    pub driving_input: usize,
    pub driving_input_relation_name: String,
    pub columns: ReplayColumns,
    pub lineage: ReplayLineage,
    pub propagated: Vec<usize>,
    pub aggregate: bool,
    pub replay_settings: Vec<(String, String)>,
}

/// `(relation name, model key, resource kind)`.
pub type RelationOperation = (String, usize, ResourceKind);

#[derive(Debug, PartialEq, Eq)]
pub struct DirectPlan {
    pub user_scope: Vec<usize>,
    pub execution_scope: Vec<usize>,
    pub prerequisite_execution_scope: Vec<usize>,
    pub prerequisites: Vec<Prerequisite>,
    pub entries: Vec<Entry>,
    pub replay_roots: Vec<ReplayRoot>,
    pub teardown: Vec<RelationOperation>,
    pub creation: Vec<RelationOperation>,
    /// Indexes into `mutable_ref_names`, by name, of table targets that read mutable side refs.
    pub mutable_ref_warnings: Vec<usize>,
}

struct Planner<'i, 'a> {
    input: &'i DirectInput<'a>,
    existing: HashSet<&'a str>,
    prerequisite_scope: Vec<usize>,
    execution_scope: Vec<usize>,
    executed: HashSet<usize>,
    driving_parent: HashMap<usize, usize>,
}

/// Plan the complete selected downstream closure without change pruning.
///
/// # Errors
/// Returns the message of the first rule the selection or the warehouse violates.
pub fn plan_direct_build(input: &DirectInput<'_>) -> Result<DirectPlan, String> {
    Planner::new(input).build()
}

impl<'i, 'a> Planner<'i, 'a> {
    fn new(input: &'i DirectInput<'a>) -> Self {
        let existing: HashSet<&'a str> =
            input.catalog.iter().map(|relation| relation.name).collect();
        let normal_scope = normal_execution_scope(input);
        let prerequisite_scope = if input.include_missing_upstream {
            prerequisite_execution_scope(input, &normal_scope, &existing)
        } else {
            Vec::new()
        };
        let in_scope: HashSet<usize> = normal_scope
            .iter()
            .chain(&prerequisite_scope)
            .copied()
            .collect();
        let execution_scope: Vec<usize> = (0..input.nodes.len())
            .filter(|key| in_scope.contains(key) && is_model(&input.nodes[*key]))
            .collect();
        let executed: HashSet<usize> = execution_scope.iter().copied().collect();
        let mut driving_parent = HashMap::new();
        for (key, node) in input.nodes.iter().enumerate() {
            for &(upstream, driving) in &node.upstream {
                if driving {
                    driving_parent.insert(key, upstream);
                }
            }
        }
        Self {
            input,
            existing,
            prerequisite_scope,
            execution_scope,
            executed,
            driving_parent,
        }
    }

    fn node(&self, key: usize) -> &'i Node<'a> {
        &self.input.nodes[key]
    }

    fn build(&self) -> Result<DirectPlan, String> {
        self.reject_adopted_source_target_collisions()?;
        self.reject_out_of_scope_driving_consumers()?;
        let prerequisites = self.prerequisites();
        self.reject_missing_prerequisites(&prerequisites)?;
        let entries = self
            .execution_scope
            .iter()
            .map(|&key| self.entry(key))
            .collect::<Result<Vec<_>, _>>()?;
        let replay_roots = self.replay_roots()?;
        self.reject_incompatible_replay_inputs(&replay_roots)?;
        let mut teardown = Vec::new();
        for kind in [
            ResourceKind::View,
            ResourceKind::MaterializedView,
            ResourceKind::Table,
        ] {
            teardown.extend(relation_operations(entries.iter().rev(), kind));
        }
        let mut creation = Vec::new();
        for kind in [
            ResourceKind::Table,
            ResourceKind::MaterializedView,
            ResourceKind::View,
        ] {
            creation.extend(relation_operations(entries.iter(), kind));
        }
        let mutable_ref_warnings = self.mutable_ref_warnings(&entries);
        Ok(DirectPlan {
            user_scope: self
                .execution_scope
                .iter()
                .copied()
                .filter(|key| self.input.selected.contains(key))
                .collect(),
            execution_scope: self.execution_scope.clone(),
            prerequisite_execution_scope: self.prerequisite_scope.clone(),
            prerequisites,
            entries,
            replay_roots,
            teardown,
            creation,
            mutable_ref_warnings,
        })
    }

    fn model_relations(&self, key: usize) -> Result<Vec<(String, ResourceKind)>, String> {
        let resources = &self.node(key).resources;
        let named = |wanted: ResourceKind| -> Vec<&str> {
            resources
                .iter()
                .filter(|(_, kind)| *kind == Some(wanted))
                .map(|(name, _)| *name)
                .collect()
        };
        let (tables, views, ordinary_views) = (
            named(ResourceKind::Table),
            named(ResourceKind::MaterializedView),
            named(ResourceKind::View),
        );
        if ordinary_views.len() == 1 && tables.is_empty() && views.is_empty() {
            return Ok(vec![(ordinary_views[0].to_owned(), ResourceKind::View)]);
        }
        if tables.len() != 1 || views.len() != 1 || !ordinary_views.is_empty() {
            return Err(format!(
                "Direct plan received invalid realized resources for '{}'",
                self.node(key).name
            ));
        }
        Ok(vec![
            (tables[0].to_owned(), ResourceKind::Table),
            (views[0].to_owned(), ResourceKind::MaterializedView),
        ])
    }

    fn reject_adopted_source_target_collisions(&self) -> Result<(), String> {
        let mut targets: HashSet<String> = HashSet::new();
        for &key in &self.execution_scope {
            targets.extend(self.model_relations(key)?.into_iter().map(|(name, _)| name));
        }
        let mut collisions: Vec<&str> = self
            .input
            .external_columns
            .iter()
            .map(|(name, _)| *name)
            .filter(|name| targets.contains(*name))
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        collisions.sort_unstable();
        if collisions.is_empty() {
            return Ok(());
        }
        Err(format!(
            "Direct mode refuses adopted source relations that collide with managed targets: {}",
            collisions.join(", ")
        ))
    }

    fn reject_out_of_scope_driving_consumers(&self) -> Result<(), String> {
        let rebuilt: HashSet<&str> = self
            .prerequisite_scope
            .iter()
            .map(|&key| self.node(key).relation_name)
            .collect();
        if rebuilt.is_empty() {
            return Ok(());
        }
        let in_scope_views: HashSet<&str> = self
            .execution_scope
            .iter()
            .flat_map(|&key| &self.node(key).resources)
            .filter(|(_, kind)| *kind == Some(ResourceKind::MaterializedView))
            .map(|(name, _)| *name)
            .collect();
        let mut consumers: Vec<&str> = self
            .input
            .catalog
            .iter()
            .filter(|relation| {
                relation.materialized_view
                    && !in_scope_views.contains(relation.name)
                    && relation
                        .source_relation_names
                        .iter()
                        .any(|source| rebuilt.contains(source))
            })
            .map(|relation| relation.name)
            .collect();
        consumers.sort_unstable();
        if consumers.is_empty() {
            return Ok(());
        }
        Err(format!(
            "Direct plan cannot rebuild missing upstream models while out-of-scope materialized \
             views consume them: {}; include or remove those consumers before retrying",
            consumers.join(", ")
        ))
    }

    fn prerequisites(&self) -> Vec<Prerequisite> {
        let mut prerequisite_keys = HashSet::new();
        for &key in &self.execution_scope {
            for &(upstream, _) in &self.node(key).upstream {
                if !self.executed.contains(&upstream) {
                    prerequisite_keys.insert(upstream);
                }
            }
        }
        (0..self.input.nodes.len())
            .filter(|key| prerequisite_keys.contains(key))
            .map(|key| {
                let node = self.node(key);
                Prerequisite {
                    key,
                    relation_name: node.relation_name.to_owned(),
                    present: self.existing.contains(node.relation_name),
                    framework_managed: matches!(
                        node.kind,
                        NodeKind::Source { framework_managed } if framework_managed || node.postgres_source_name
                    ),
                }
            })
            .collect()
    }

    fn reject_missing_prerequisites(&self, prerequisites: &[Prerequisite]) -> Result<(), String> {
        let details: Vec<String> = prerequisites
            .iter()
            .filter(|prerequisite| !prerequisite.present && !prerequisite.framework_managed)
            .map(
                |prerequisite| match self.node(prerequisite.key).producing_pipeline {
                    None => prerequisite.relation_name.clone(),
                    Some(pipeline) => format!(
                        "{} (built by pipeline {pipeline}; add it to the selection)",
                        prerequisite.relation_name
                    ),
                },
            )
            .collect();
        if details.is_empty() {
            return Ok(());
        }
        Err(format!(
            "Direct plan requires preserved upstream relations that do not exist: {}",
            details.join(", ")
        ))
    }

    fn require_driving_parent(&self, key: usize) -> Result<usize, String> {
        self.driving_parent.get(&key).copied().ok_or_else(|| {
            format!(
                "Direct plan cannot find the driving input of model '{}'",
                self.node(key).name
            )
        })
    }

    fn reason(&self, key: usize) -> &'static str {
        if self.input.selection_mode == SelectionMode::AllModels {
            return "all_models";
        }
        if self.prerequisite_scope.contains(&key) {
            return "missing_upstream";
        }
        if self.input.source_drift.contains(&key) {
            return "source_drift";
        }
        if self.input.selected.contains(&key) {
            return if self.input.selection_mode == SelectionMode::Changed {
                "changed"
            } else {
                "selected"
            };
        }
        "downstream_of_selected"
    }

    fn entry(&self, key: usize) -> Result<Entry, String> {
        let node = self.node(key);
        let mut driving_input = self.driving_parent.get(&key).copied();
        let relations = self.model_relations(key)?;
        let (table, query) = match node.kind {
            NodeKind::Model { table, query, .. } => (table, query),
            NodeKind::Source { .. } => (false, ""),
        };
        if table && driving_input.is_none() {
            driving_input = Some(self.require_driving_parent(key)?);
        }
        Ok(Entry {
            key,
            reason: self.reason(key),
            relations,
            driving_input,
            is_replay_root: table
                && driving_input.is_none_or(|parent| !self.executed.contains(&parent)),
            sql_change: self.sql_change(node.name, query),
        })
    }

    fn sql_change(&self, name: &str, current_sql: &str) -> SqlChange {
        let current_hash = sha256_hex(current_sql);
        let baselines = match &self.input.fingerprints {
            Fingerprints::Unavailable { warning } => {
                return SqlChange {
                    status: "baseline_unavailable",
                    current_hash,
                    previous: None,
                    unified_diff: None,
                    warning: warning.map(str::to_owned),
                };
            }
            Fingerprints::Available(baselines) => baselines,
        };
        let identity = format!("{}.{name}", self.input.database);
        let Some(&(_, previous_sql, previous_hash)) = baselines
            .iter()
            .find(|(baseline, _, _)| *baseline == identity)
        else {
            return SqlChange {
                status: "first_baseline",
                current_hash,
                previous: None,
                unified_diff: None,
                warning: None,
            };
        };
        let changed = previous_hash != current_hash;
        SqlChange {
            status: if changed {
                "query_changed"
            } else {
                "no_query_change"
            },
            unified_diff: changed.then(|| {
                let (previous, current) = (
                    split_lines(previous_sql, true),
                    split_lines(current_sql, true),
                );
                unified_diff(&previous, &current, "previous", "current", "\n").concat()
            }),
            current_hash,
            previous: Some((previous_sql.to_owned(), previous_hash.to_owned())),
            warning: None,
        }
    }

    fn replay_roots(&self) -> Result<Vec<ReplayRoot>, String> {
        let mut root_by_key: HashMap<usize, usize> = HashMap::new();
        let mut propagated_by_root: HashMap<usize, Vec<usize>> = HashMap::new();
        for &key in &self.execution_scope {
            let parent = self.driving_parent.get(&key).copied();
            let root = parent
                .filter(|parent| self.executed.contains(parent))
                .and_then(|parent| root_by_key.get(&parent).copied())
                .unwrap_or(key);
            root_by_key.insert(key, root);
            if !matches!(self.node(key).kind, NodeKind::Model { table: true, .. }) {
                continue;
            }
            let root_parent = self.driving_parent.get(&root).copied();
            if root_parent.is_some_and(|parent| self.node(parent).postgres_source_name) {
                continue;
            }
            propagated_by_root.entry(root).or_default().push(key);
        }
        self.execution_scope
            .iter()
            .filter_map(|root| propagated_by_root.remove_entry(root))
            .map(|(root, propagated)| self.replay_root(root, propagated))
            .collect()
    }

    fn replay_root(&self, key: usize, propagated: Vec<usize>) -> Result<ReplayRoot, String> {
        let driving_input = self.require_driving_parent(key)?;
        let node = self.node(key);
        let NodeKind::Model {
            table: true,
            aggregate,
            ref replay_settings,
            ..
        } = node.kind
        else {
            return Err(format!("Replay root '{}' is not a table model", node.name));
        };
        let lineage = node.replay_lineage.ok_or_else(|| {
            format!(
                "Direct plan cannot resolve the replay mode of '{}'",
                node.name
            )
        })?;
        let mut replay_settings: Vec<(String, String)> = replay_settings
            .iter()
            .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
            .collect();
        replay_settings.sort();
        Ok(ReplayRoot {
            key,
            driving_input,
            driving_input_relation_name: self.node(driving_input).relation_name.to_owned(),
            columns: self.replay_columns(driving_input),
            lineage,
            propagated,
            aggregate,
            replay_settings,
        })
    }

    fn replay_columns(&self, driving_input: usize) -> ReplayColumns {
        let node = self.node(driving_input);
        let external = matches!(node.kind, NodeKind::Source { .. })
            .then(|| {
                self.input
                    .external_columns
                    .iter()
                    .rev()
                    .find(|(name, _)| *name == node.relation_name)
                    .map(|(_, columns)| columns)
            })
            .flatten();
        let default = ExternalColumns::default();
        let columns = external.unwrap_or(&default);
        ReplayColumns {
            partition: columns.partition.unwrap_or(PARTITION_COLUMN).to_owned(),
            offset: columns.offset.unwrap_or(OFFSET_COLUMN).to_owned(),
            timestamp: columns.timestamp.unwrap_or(TIMESTAMP_COLUMN).to_owned(),
            landed_at: columns
                .landed_at
                .or(columns.timestamp)
                .unwrap_or(LANDED_AT_COLUMN)
                .to_owned(),
            cursor: columns.cursor.unwrap_or(CURSOR_COLUMN).to_owned(),
        }
    }

    fn reject_incompatible_replay_inputs(&self, roots: &[ReplayRoot]) -> Result<(), String> {
        for root in roots {
            let Some(relation) = self
                .input
                .catalog
                .iter()
                .find(|relation| relation.name == root.driving_input_relation_name)
            else {
                continue;
            };
            let missing: Vec<&str> = self
                .required_replay_columns(root)
                .into_iter()
                .filter(|column| !relation.columns.contains(column))
                .collect();
            if missing.is_empty() {
                continue;
            }
            return Err(format!(
                "Direct plan cannot replay '{}' from preserved upstream relation '{}': required \
                 replay columns are missing: {}. Rebuild the upstream scope in the same \
                 invocation (for example, --select {}) or select its pipeline.",
                self.node(root.key).name,
                root.driving_input_relation_name,
                missing.join(", "),
                self.node(root.driving_input).name,
            ));
        }
        Ok(())
    }

    fn required_replay_columns<'r>(&self, root: &'r ReplayRoot) -> Vec<&'r str> {
        let columns = &root.columns;
        let mut required: Vec<&str> = match root.lineage {
            ReplayLineage::Offsets => vec![&columns.partition, &columns.offset],
            ReplayLineage::Timestamp => vec![&columns.timestamp],
            ReplayLineage::LandedAt => vec![&columns.landed_at],
            ReplayLineage::Cursor => vec![&columns.cursor],
        };
        if self.input.has_start_time {
            match root.lineage {
                ReplayLineage::Offsets => required.push(if columns.landed_at.is_empty() {
                    &columns.timestamp
                } else {
                    &columns.landed_at
                }),
                ReplayLineage::Cursor => required.push(&columns.timestamp),
                ReplayLineage::Timestamp | ReplayLineage::LandedAt => {}
            }
        }
        let mut seen = HashSet::new();
        required
            .into_iter()
            .filter(|column| !column.is_empty() && seen.insert(*column))
            .collect()
    }

    fn mutable_ref_warnings(&self, entries: &[Entry]) -> Vec<usize> {
        let tables: HashSet<&str> = entries
            .iter()
            .flat_map(|entry| &entry.relations)
            .filter(|(_, kind)| *kind == ResourceKind::Table)
            .map(|(name, _)| name.as_str())
            .collect();
        let names = &self.input.mutable_ref_names;
        let mut indexes: Vec<usize> = (0..names.len()).collect();
        indexes.sort_by_key(|&index| names[index]);
        indexes.retain(|&index| tables.contains(names[index]));
        indexes
    }
}

fn is_model(node: &Node<'_>) -> bool {
    matches!(node.kind, NodeKind::Model { .. })
}

fn model_keys(input: &DirectInput<'_>, keys: impl Iterator<Item = usize>) -> Vec<usize> {
    keys.filter(|&key| is_model(&input.nodes[key])).collect()
}

fn normal_execution_scope(input: &DirectInput<'_>) -> Vec<usize> {
    if input.selection_mode == SelectionMode::AllModels {
        return model_keys(input, 0..input.nodes.len());
    }
    if input.selected.is_empty() {
        return Vec::new();
    }
    let mut visited: HashSet<usize> = input.selected.clone();
    let mut stack: Vec<usize> = input.selected.iter().copied().collect();
    while let Some(key) = stack.pop() {
        for &downstream in &input.nodes[key].downstream {
            if visited.insert(downstream) {
                stack.push(downstream);
            }
        }
    }
    model_keys(
        input,
        (0..input.nodes.len()).filter(|key| visited.contains(key)),
    )
}

fn prerequisite_execution_scope(
    input: &DirectInput<'_>,
    normal_scope: &[usize],
    existing: &HashSet<&str>,
) -> Vec<usize> {
    let mut execution: HashSet<usize> = normal_scope.iter().copied().collect();
    let mut added: HashSet<usize> = HashSet::new();
    loop {
        let missing: HashSet<usize> = execution
            .iter()
            .flat_map(|&key| &input.nodes[key].upstream)
            .map(|&(upstream, _)| upstream)
            .filter(|upstream| {
                !execution.contains(upstream)
                    && is_model(&input.nodes[*upstream])
                    && !existing.contains(input.nodes[*upstream].relation_name)
            })
            .collect();
        if missing.is_empty() {
            break;
        }
        execution.extend(&missing);
        added.extend(missing);
    }
    model_keys(
        input,
        (0..input.nodes.len()).filter(|key| added.contains(key)),
    )
}

fn relation_operations<'e>(
    entries: impl Iterator<Item = &'e Entry>,
    kind: ResourceKind,
) -> Vec<RelationOperation> {
    entries
        .flat_map(|entry| {
            entry
                .relations
                .iter()
                .filter(move |(_, relation_kind)| *relation_kind == kind)
                .map(|(name, _)| (name.clone(), entry.key, kind))
        })
        .collect()
}

/// The lowercase hex SHA-256 of `text`.
#[must_use]
pub fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .fold(String::with_capacity(64), |mut hex, byte| {
            let _ = write!(hex, "{byte:02x}");
            hex
        })
}

/// `str.splitlines(keepends)`, splitting at every boundary Python recognizes.
#[must_use]
pub fn split_lines(text: &str, keep_ends: bool) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut characters = text.char_indices().peekable();
    while let Some((index, character)) = characters.next() {
        let mut end = index + character.len_utf8();
        match character {
            '\r' => {
                if let Some(&(next, '\n')) = characters.peek() {
                    characters.next();
                    end = next + 1;
                }
            }
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => {}
            _ => continue,
        }
        lines.push(&text[start..if keep_ends { end } else { index }]);
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(name: &'static str) -> Node<'static> {
        Node {
            name,
            kind: NodeKind::Source {
                framework_managed: false,
            },
            relation_name: name,
            resources: Vec::new(),
            replay_lineage: Some(ReplayLineage::Offsets),
            postgres_source_name: false,
            producing_pipeline: None,
            upstream: Vec::new(),
            downstream: Vec::new(),
        }
    }

    fn table(
        name: &'static str,
        target: &'static str,
        mv: &'static str,
        upstream: usize,
    ) -> Node<'static> {
        Node {
            name,
            kind: NodeKind::Model {
                table: true,
                aggregate: false,
                query: "select 1\n",
                replay_settings: vec![("b", "2"), ("a", "1")],
            },
            relation_name: target,
            resources: vec![
                (target, Some(ResourceKind::Table)),
                (mv, Some(ResourceKind::MaterializedView)),
            ],
            replay_lineage: Some(ReplayLineage::Offsets),
            postgres_source_name: false,
            producing_pipeline: Some("orders_pipeline"),
            upstream: vec![(upstream, true)],
            downstream: Vec::new(),
        }
    }

    fn input(nodes: Vec<Node<'static>>, selected: &[usize]) -> DirectInput<'static> {
        DirectInput {
            nodes,
            database: "analytics",
            selection_mode: SelectionMode::Explicit,
            selected: selected.iter().copied().collect(),
            source_drift: HashSet::new(),
            include_missing_upstream: false,
            has_start_time: false,
            catalog: Vec::new(),
            external_columns: Vec::new(),
            fingerprints: Fingerprints::Available(Vec::new()),
            mutable_ref_names: Vec::new(),
        }
    }

    fn chain() -> Vec<Node<'static>> {
        let mut nodes = vec![
            source("raw_orders"),
            table("orders", "orders", "orders_mv", 0),
            table("order_totals", "order_totals", "order_totals_mv", 1),
        ];
        nodes[0].downstream = vec![1];
        nodes[1].downstream = vec![2];
        nodes
    }

    #[test]
    fn replays_a_selected_chain_from_its_first_table_and_orders_operations() {
        let mut input = input(chain(), &[1]);
        input.catalog.push(CatalogRelation {
            name: "raw_orders",
            materialized_view: false,
            columns: vec![PARTITION_COLUMN, OFFSET_COLUMN],
            source_relation_names: Vec::new(),
        });
        let plan = plan_direct_build(&input).unwrap();
        assert_eq!(plan.execution_scope, [1, 2]);
        assert_eq!(plan.user_scope, [1]);
        let reasons: Vec<&str> = plan.entries.iter().map(|entry| entry.reason).collect();
        assert_eq!(reasons, ["selected", "downstream_of_selected"]);
        assert_eq!(plan.replay_roots.len(), 1);
        assert_eq!(plan.replay_roots[0].propagated, [1, 2]);
        assert_eq!(plan.replay_roots[0].replay_settings[0].0, "a");
        assert!(plan.entries[0].is_replay_root && !plan.entries[1].is_replay_root);
        let teardown: Vec<&str> = plan
            .teardown
            .iter()
            .map(|(name, _, _)| name.as_str())
            .collect();
        assert_eq!(
            teardown,
            ["order_totals_mv", "orders_mv", "order_totals", "orders"]
        );
        let creation: Vec<&str> = plan
            .creation
            .iter()
            .map(|(name, _, _)| name.as_str())
            .collect();
        assert_eq!(
            creation,
            ["orders", "order_totals", "orders_mv", "order_totals_mv"]
        );
        assert_eq!(plan.entries[0].sql_change.status, "first_baseline");
    }

    #[test]
    fn rebuilds_missing_upstream_models_and_names_their_pipeline_otherwise() {
        let mut missing = input(chain(), &[2]);
        let error = plan_direct_build(&missing).unwrap_err();
        assert_eq!(
            error,
            "Direct plan requires preserved upstream relations that do not exist: orders \
             (built by pipeline orders_pipeline; add it to the selection)"
        );
        missing.include_missing_upstream = true;
        let error = plan_direct_build(&missing).unwrap_err();
        assert_eq!(
            error,
            "Direct plan requires preserved upstream relations that do not exist: raw_orders"
        );
        missing.catalog.push(CatalogRelation {
            name: "raw_orders",
            materialized_view: false,
            columns: vec![PARTITION_COLUMN],
            source_relation_names: Vec::new(),
        });
        let error = plan_direct_build(&missing).unwrap_err();
        assert!(
            error.contains("required replay columns are missing: _replay_offset."),
            "{error}"
        );
        assert!(error.contains("--select raw_orders"), "{error}");
    }

    #[test]
    fn reports_query_changes_against_the_applied_baseline() {
        let mut input = input(chain(), &[1]);
        let previous_hash = sha256_hex("select 0\n");
        let current_hash = sha256_hex("select 1\n");
        input.fingerprints = Fingerprints::Available(vec![
            ("analytics.orders", "select 0\n", &previous_hash),
            ("analytics.order_totals", "select 1\n", &current_hash),
        ]);
        input.catalog.push(CatalogRelation {
            name: "raw_orders",
            materialized_view: false,
            columns: vec![PARTITION_COLUMN, OFFSET_COLUMN],
            source_relation_names: Vec::new(),
        });
        let plan = plan_direct_build(&input).unwrap();
        let change = &plan.entries[0].sql_change;
        assert_eq!(change.status, "query_changed");
        assert_eq!(
            change.unified_diff.as_deref(),
            Some("--- previous\n+++ current\n@@ -1 +1 @@\n-select 0\n+select 1\n")
        );
        assert_eq!(plan.entries[1].sql_change.status, "no_query_change");
    }

    #[test]
    fn splits_lines_like_python() {
        assert_eq!(
            split_lines("a\r\nb\rc\u{2028}d", true),
            ["a\r\n", "b\r", "c\u{2028}", "d"]
        );
        assert_eq!(split_lines("a\n\nb\n", false), ["a", "", "b"]);
        assert!(split_lines("", true).is_empty());
    }
}
