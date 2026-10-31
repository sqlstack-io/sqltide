//! Python bindings for deployment planning over desired state.

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_planner::desired::{
    CycleError, DesiredGraph, DesiredObject, KeyInfo, ObjectKind, ObjectType, ReplayMode,
    ReplayPolicy, ReplayRule,
};
use sqltide_planner::diff::{
    Classification, Column, Comparison, Differ, Kafka, Settings, Spec, Storage,
};
use sqltide_planner::rebuild::{
    ChangeType, ObjectChange, RebuildSubtree, SchemaChangeKind, SeedCompatibility, rebuild_subtrees,
};

/// `(mode, lookback seconds)`.
type RuleArgument = (String, Option<i64>);
/// `(key, deps, kind, materialized view source key, materialized view target,
/// (breaking rule, non-breaking rule) for a table with an authored policy)`.
type ObjectArgument = (
    usize,
    Vec<usize>,
    String,
    Option<usize>,
    Option<String>,
    Option<(Option<RuleArgument>, Option<RuleArgument>)>,
);
/// `(key, change type, force full refresh, forced start time, schema change kind, seed
/// compatibility)`.
type ChangeArgument = (
    usize,
    String,
    bool,
    Option<String>,
    Option<String>,
    Option<String>,
);
/// `(root, affected, upstream boundary, replay required, execution mode, forced full refresh,
/// forced start time, configured backfill mode, execution lookback seconds)`.
type SubtreeResult = (
    usize,
    Vec<usize>,
    usize,
    bool,
    &'static str,
    bool,
    Option<String>,
    Option<&'static str>,
    Option<i64>,
);

/// `(kind, [(name, type, default)], texts, order by, settings)`, where the texts are the
/// engine, partition, and TTL of a table; the broker list, topic, consumer group, and format of a
/// Kafka table; the source, target, and query of a materialized view; or a view's query.
type SpecArgument = (
    String,
    Vec<(String, String, Option<String>)>,
    Vec<Option<String>>,
    Vec<String>,
    Option<Vec<(String, String)>>,
);

fn spec(argument: SpecArgument) -> PyResult<Spec> {
    let (kind, columns, texts, order_by, settings) = argument;
    let columns: Vec<Column> = columns
        .into_iter()
        .map(|(name, type_sql, default)| Column {
            name,
            type_sql,
            default,
        })
        .collect();
    let settings: Settings = settings.map(|entries| entries.into_iter().collect());
    let invalid = || PyValueError::new_err(format!("Invalid {kind} spec"));
    let required = |text: Option<String>| text.ok_or_else(invalid);
    let mut texts = texts.into_iter();
    let mut next = || texts.next().ok_or_else(invalid);
    Ok(match kind.as_str() {
        "table" => Spec::Table {
            columns,
            storage: Storage {
                engine: required(next()?)?,
                order_by,
                partition_by: next()?,
                ttl: next()?,
                settings,
            },
        },
        "kafka_table" => Spec::KafkaTable {
            columns,
            kafka: Kafka {
                broker_list: required(next()?)?,
                topic: required(next()?)?,
                consumer_group: required(next()?)?,
                format: required(next()?)?,
                settings,
            },
        },
        "materialized_view" => Spec::MaterializedView {
            source: required(next()?)?,
            target: required(next()?)?,
            query: required(next()?)?,
        },
        "view" => Spec::View {
            query: required(next()?)?,
        },
        _ => return Err(PyValueError::new_err(format!("Unknown spec kind '{kind}'"))),
    })
}

/// `(change type, schema change kind, seed compatibility)`.
type ClassificationResult = (&'static str, Option<&'static str>, Option<&'static str>);

fn classification_result(classification: Classification) -> ClassificationResult {
    let difference = classification.column_difference;
    (
        match classification.change_type {
            ChangeType::Create => "create",
            ChangeType::Rebuild => "rebuild",
            ChangeType::Replace => "replace",
            ChangeType::NoOp => "no_op",
        },
        difference.map(|difference| match difference.schema_change_kind() {
            SchemaChangeKind::NonBreaking => "non_breaking",
            SchemaChangeKind::Breaking => "breaking",
        }),
        difference.map(|difference| match difference.seed_compatibility() {
            SeedCompatibility::Seedable => "seedable",
            SeedCompatibility::NonSeedable => "non_seedable",
        }),
    )
}

/// Classify `(desired spec, actual spec | None, forced)` comparisons as
/// `(change type, schema change kind, seed compatibility)`. A type that cannot be normalized
/// raises `ValueError`.
#[pyfunction]
fn classify_object_changes(
    py: Python<'_>,
    comparisons: Vec<(SpecArgument, Option<SpecArgument>, bool)>,
) -> PyResult<Vec<ClassificationResult>> {
    let comparisons: Vec<Comparison> = comparisons
        .into_iter()
        .map(|(desired, actual, forced)| {
            Ok(Comparison {
                desired: spec(desired)?,
                actual: actual.map(spec).transpose()?,
                forced,
            })
        })
        .collect::<PyResult<_>>()?;
    py.detach(|| Differ::new().classify_all(&comparisons))
        .map(|classifications| {
            classifications
                .into_iter()
                .map(classification_result)
                .collect()
        })
        .map_err(|error| PyValueError::new_err(error.message))
}

fn replay_mode(text: &str) -> PyResult<ReplayMode> {
    match text {
        "full" => Ok(ReplayMode::Full),
        "bounded" => Ok(ReplayMode::Bounded),
        other => Err(PyValueError::new_err(format!(
            "Unknown replay mode '{other}'"
        ))),
    }
}

fn replay_rule(rule: Option<RuleArgument>) -> PyResult<Option<ReplayRule>> {
    rule.map(|(mode, lookback_seconds)| {
        Ok(ReplayRule {
            mode: replay_mode(&mode)?,
            lookback_seconds,
        })
    })
    .transpose()
}

fn object_kind(
    kind: &str,
    source: Option<usize>,
    target: Option<String>,
    policy: Option<(Option<RuleArgument>, Option<RuleArgument>)>,
) -> PyResult<ObjectKind> {
    match (kind, source, target) {
        ("kafka_table", _, _) => Ok(ObjectKind::KafkaTable),
        ("view", _, _) => Ok(ObjectKind::View),
        ("table", _, _) => Ok(ObjectKind::Table(
            policy
                .map(|(breaking, non_breaking)| {
                    Ok::<_, PyErr>(ReplayPolicy {
                        breaking: replay_rule(breaking)?,
                        non_breaking: replay_rule(non_breaking)?,
                    })
                })
                .transpose()?,
        )),
        ("materialized_view", Some(source), Some(target)) => {
            Ok(ObjectKind::MaterializedView { source, target })
        }
        (other, _, _) => Err(PyValueError::new_err(format!(
            "Invalid desired object kind '{other}'"
        ))),
    }
}

fn object_change(change: ChangeArgument) -> PyResult<ObjectChange> {
    let (key, change_type, force_full_refresh, forced_start_time, schema, seed) = change;
    Ok(ObjectChange {
        key,
        change_type: match change_type.as_str() {
            "create" => ChangeType::Create,
            "rebuild" => ChangeType::Rebuild,
            "replace" => ChangeType::Replace,
            "no_op" => ChangeType::NoOp,
            other => {
                return Err(PyValueError::new_err(format!(
                    "Unknown change type '{other}'"
                )));
            }
        },
        force_full_refresh,
        forced_start_time,
        schema_change_kind: match schema.as_deref() {
            None => None,
            Some("non_breaking") => Some(SchemaChangeKind::NonBreaking),
            Some("breaking") => Some(SchemaChangeKind::Breaking),
            Some(other) => {
                return Err(PyValueError::new_err(format!(
                    "Unknown schema change '{other}'"
                )));
            }
        },
        seed_compatibility: match seed.as_deref() {
            None => None,
            Some("seedable") => Some(SeedCompatibility::Seedable),
            Some("non_seedable") => Some(SeedCompatibility::NonSeedable),
            Some(other) => {
                return Err(PyValueError::new_err(format!(
                    "Unknown seed compatibility '{other}'"
                )));
            }
        },
    })
}

fn subtree_result(subtree: RebuildSubtree) -> SubtreeResult {
    (
        subtree.root,
        subtree.affected,
        subtree.upstream_boundary,
        subtree.replay_required,
        subtree.execution_mode.as_str(),
        subtree.forced_full_refresh,
        subtree.forced_start_time,
        subtree.configured_backfill_mode.map(ReplayMode::as_str),
        subtree.execution_lookback_seconds,
    )
}

fn object_type(text: &str) -> PyResult<ObjectType> {
    match text {
        "kafka_table" => Ok(ObjectType::KafkaTable),
        "materialized_view" => Ok(ObjectType::MaterializedView),
        "table" => Ok(ObjectType::Table),
        "view" => Ok(ObjectType::View),
        other => Err(PyValueError::new_err(format!(
            "Unknown desired object type '{other}'"
        ))),
    }
}

fn cycle(error: CycleError) -> PyErr {
    PyValueError::new_err(error.message)
}

/// The desired-state graph over caller-interned key indices.
#[pyclass(name = "DesiredGraph", module = "sqltide._native", frozen)]
struct PyDesiredGraph {
    graph: DesiredGraph,
}

#[pymethods]
impl PyDesiredGraph {
    /// `keys` holds `(object type, name)` per index; see [`ObjectArgument`] for `objects`.
    #[new]
    fn new(
        keys: Vec<(String, String)>,
        objects: Vec<ObjectArgument>,
        replay_anchors: Vec<usize>,
    ) -> PyResult<Self> {
        let keys: Vec<KeyInfo> = keys
            .into_iter()
            .map(|(kind, name)| {
                Ok(KeyInfo {
                    object_type: object_type(&kind)?,
                    name,
                })
            })
            .collect::<PyResult<_>>()?;
        let count: usize = keys.len();
        let in_range = |index: &usize| *index < count;
        if !objects.iter().all(|(key, deps, _, source, _, _)| {
            in_range(key) && deps.iter().all(in_range) && source.as_ref().is_none_or(in_range)
        }) || !replay_anchors.iter().all(in_range)
        {
            return Err(PyValueError::new_err(
                "Desired graph key index out of range",
            ));
        }
        let objects: Vec<DesiredObject> = objects
            .into_iter()
            .map(|(key, deps, kind, source, target, policy)| {
                Ok(DesiredObject {
                    key,
                    deps,
                    kind: object_kind(&kind, source, target, policy)?,
                })
            })
            .collect::<PyResult<_>>()?;
        Ok(Self {
            graph: DesiredGraph::new(keys, objects, &replay_anchors),
        })
    }

    /// `(upstream key, downstream keys)` for every key with a downstream object.
    fn reverse_deps(&self) -> Vec<(usize, Vec<usize>)> {
        self.graph
            .reverse_dep_keys()
            .into_iter()
            .map(|key| (key, self.graph.reverse_deps(key).to_vec()))
            .collect()
    }

    /// Included keys in dependency order. A cycle raises `ValueError`.
    fn order(&self, py: Python<'_>, included: HashSet<usize>) -> PyResult<Vec<usize>> {
        py.detach(|| self.graph.order(&included)).map_err(cycle)
    }

    /// `root` and its downstream objects in dependency order. A cycle raises `ValueError`.
    fn descendants(&self, py: Python<'_>, root: usize) -> PyResult<Vec<usize>> {
        py.detach(|| self.graph.descendants(root)).map_err(cycle)
    }

    /// The rebuild subtrees `changes` require. A cycle raises `ValueError`.
    fn rebuild_subtrees(
        &self,
        py: Python<'_>,
        changes: Vec<ChangeArgument>,
    ) -> PyResult<Vec<SubtreeResult>> {
        let changes: Vec<ObjectChange> = changes
            .into_iter()
            .map(object_change)
            .collect::<PyResult<_>>()?;
        if changes
            .iter()
            .any(|change| change.key >= self.graph.key_count())
        {
            return Err(PyValueError::new_err(
                "Desired graph key index out of range",
            ));
        }
        py.detach(|| rebuild_subtrees(&self.graph, &changes))
            .map(|subtrees| subtrees.into_iter().map(subtree_result).collect())
            .map_err(cycle)
    }

    /// The nearest upstream replay anchor for `root`. A cycle raises `ValueError`.
    fn nearest_replay_anchor(
        &self,
        root: usize,
        allow_root: bool,
        root_name: &str,
    ) -> PyResult<usize> {
        self.graph
            .nearest_replay_anchor(root, allow_root, root_name)
            .map_err(cycle)
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(classify_object_changes, module)?)?;
    module.add_class::<PyDesiredGraph>()
}
