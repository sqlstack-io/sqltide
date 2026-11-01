//! Python bindings for deployment planning over desired state.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_planner::actual::{
    ActualSource, ActualStateLookup, DesiredFacts, DesiredObjectFacts, InspectionFacts,
    RecordFacts, RootFacts, actual_sources,
};
use sqltide_planner::deployment::{PlanKey, PlanSubtree, stage_deployment as stage};
use sqltide_planner::desired::{
    CycleError, DesiredGraph, DesiredObject, KeyInfo, ObjectKind, ObjectType, ReplayMode,
    ReplayPolicy, ReplayRule,
};
use sqltide_planner::diff::{
    Classification, Column, Comparison, Differ, Kafka, Settings, Spec, Storage,
};
use sqltide_planner::direct::{
    CatalogRelation, DirectInput, DirectPlan, Entry, ExternalColumns, Fingerprints, Node, NodeKind,
    ReplayLineage, ResourceKind, SelectionMode, plan_direct_build as plan_direct, split_lines,
};
use sqltide_planner::direct_fingerprint;
use sqltide_planner::inspection::{
    Relation, RootState, deployment_candidate_logical_name as candidate_logical_name,
    latest_record_positions, managed_relations, root_state,
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

/// `difflib.unified_diff` over `str.splitlines(keep_ends)` of both texts, with `"\n"` line
/// terminators when the lines keep their ends and none otherwise.
#[pyfunction]
fn unified_diff(a: &str, b: &str, from_file: &str, to_file: &str, keep_ends: bool) -> Vec<String> {
    sqltide_planner::unified_diff::unified_diff(
        &split_lines(a, keep_ends),
        &split_lines(b, keep_ends),
        from_file,
        to_file,
        if keep_ends { "\n" } else { "" },
    )
}

/// `(step ID, phase, action, root, target, physical name)`.
type StepResult = (
    String,
    &'static str,
    &'static str,
    usize,
    Option<usize>,
    Option<String>,
);

/// Stage `(root, affected)` subtrees over `(object type, name, uses mutable refs)` keys into
/// `(steps, [(key, shadow physical name)], [(root, target)] mutable-ref warnings)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn stage_deployment(
    keys: Vec<(String, String, bool)>,
    subtrees: Vec<(usize, Vec<usize>)>,
    deployment_id: Option<&str>,
) -> PyResult<(Vec<StepResult>, Vec<(usize, String)>, Vec<(usize, usize)>)> {
    let count = keys.len();
    if subtrees
        .iter()
        .any(|(root, affected)| *root >= count || affected.iter().any(|key| *key >= count))
    {
        return Err(PyValueError::new_err("Deployment key index out of range"));
    }
    let keys: Vec<PlanKey<'_>> = keys
        .iter()
        .map(|(object_type, name, uses_mutable_refs)| PlanKey {
            object_type,
            name,
            uses_mutable_refs: *uses_mutable_refs,
        })
        .collect();
    let subtrees: Vec<PlanSubtree> = subtrees
        .into_iter()
        .map(|(root, affected)| PlanSubtree { root, affected })
        .collect();
    let staged = stage(&keys, &subtrees, deployment_id);
    Ok((
        staged
            .steps
            .into_iter()
            .map(|step| {
                (
                    step.step_id,
                    step.phase,
                    step.action,
                    step.root,
                    step.target,
                    step.physical_name,
                )
            })
            .collect(),
        staged.shadow_objects,
        staged.mutable_ref_warnings,
    ))
}

/// `(table, aggregate, query, replay settings)` for a model.
type ModelArgument = (bool, bool, String, Vec<(String, String)>);
/// `(name, model, source is framework managed, relation name, [(resource name, kind)], replay
/// lineage, shares a postgres source name, producing pipeline, [(upstream, driving)],
/// downstream)`.
type NodeArgument = (
    String,
    Option<ModelArgument>,
    bool,
    String,
    Vec<(String, Option<String>)>,
    Option<String>,
    bool,
    Option<String>,
    Vec<(usize, bool)>,
    Vec<usize>,
);
/// `(name, is a materialized view, columns, source relation names)`.
type CatalogArgument = (String, bool, Vec<String>, Vec<String>);
/// `(relation name, [partition, offset, timestamp, landed at, cursor] column overrides)`.
type ExternalArgument = (String, [Option<String>; 5]);
/// `(available, warning, [(identity, definition SQL, definition hash)])`.
type FingerprintArgument = (bool, Option<String>, Vec<(String, String, String)>);
/// `(status, current hash, previous SQL, previous hash, unified diff, warning)`.
type SqlChangeResult = (
    &'static str,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);
/// `(key, reason, [(relation name, kind)], driving input, is replay root, SQL change)`.
type EntryResult = (
    usize,
    &'static str,
    Vec<(String, &'static str)>,
    Option<usize>,
    bool,
    SqlChangeResult,
);
/// `(key, driving input, driving relation, [partition, offset, timestamp, landed at, cursor],
/// lineage, propagated keys, aggregate, replay settings)`.
type ReplayRootResult = (
    usize,
    usize,
    String,
    [String; 5],
    &'static str,
    Vec<usize>,
    bool,
    Vec<(String, String)>,
);
/// `(relation name, key, kind)`.
type OperationResult = (String, usize, &'static str);
/// `(user scope, execution scope, prerequisite execution scope, [(key, relation, present,
/// framework managed)], entries, replay roots, teardown, creation, mutable-ref warning indexes)`.
type DirectPlanResult = (
    Vec<usize>,
    Vec<usize>,
    Vec<usize>,
    Vec<(usize, String, bool, bool)>,
    Vec<EntryResult>,
    Vec<ReplayRootResult>,
    Vec<OperationResult>,
    Vec<OperationResult>,
    Vec<usize>,
);

fn resource_kind(kind: Option<&str>) -> PyResult<Option<ResourceKind>> {
    Ok(match kind {
        None => None,
        Some("table") => Some(ResourceKind::Table),
        Some("materialized_view") => Some(ResourceKind::MaterializedView),
        Some("view") => Some(ResourceKind::View),
        Some(other) => {
            return Err(PyValueError::new_err(format!(
                "Unknown direct resource kind '{other}'"
            )));
        }
    })
}

fn replay_lineage(text: &str) -> PyResult<ReplayLineage> {
    Ok(match text {
        "offsets" => ReplayLineage::Offsets,
        "timestamp" => ReplayLineage::Timestamp,
        "landed_at" => ReplayLineage::LandedAt,
        "cursor" => ReplayLineage::Cursor,
        other => {
            return Err(PyValueError::new_err(format!(
                "Unknown replay lineage mode '{other}'"
            )));
        }
    })
}

fn parse_selection_mode(text: &str) -> PyResult<SelectionMode> {
    Ok(match text {
        "all_models" => SelectionMode::AllModels,
        "explicit" => SelectionMode::Explicit,
        "changed" => SelectionMode::Changed,
        other => {
            return Err(PyValueError::new_err(format!(
                "Unknown direct selection mode '{other}'"
            )));
        }
    })
}

fn node(argument: &NodeArgument) -> PyResult<Node<'_>> {
    let (
        name,
        model,
        framework_managed,
        relation_name,
        resources,
        lineage,
        postgres_source_name,
        pipeline,
        upstream,
        downstream,
    ) = argument;
    Ok(Node {
        name,
        kind: match model {
            None => NodeKind::Source {
                framework_managed: *framework_managed,
            },
            Some((table, aggregate, query, settings)) => NodeKind::Model {
                table: *table,
                aggregate: *aggregate,
                query,
                replay_settings: settings
                    .iter()
                    .map(|(name, value)| (name.as_str(), value.as_str()))
                    .collect(),
            },
        },
        relation_name,
        resources: resources
            .iter()
            .map(|(name, kind)| Ok((name.as_str(), resource_kind(kind.as_deref())?)))
            .collect::<PyResult<_>>()?,
        replay_lineage: lineage.as_deref().map(replay_lineage).transpose()?,
        postgres_source_name: *postgres_source_name,
        producing_pipeline: pipeline.as_deref(),
        upstream: upstream.clone(),
        downstream: downstream.clone(),
    })
}

fn entry_result(entry: Entry) -> EntryResult {
    let change = entry.sql_change;
    let (previous_sql, previous_hash) = change.previous.unzip();
    (
        entry.key,
        entry.reason,
        entry
            .relations
            .into_iter()
            .map(|(name, kind)| (name, kind.as_str()))
            .collect(),
        entry.driving_input,
        entry.is_replay_root,
        (
            change.status,
            change.current_hash,
            previous_sql,
            previous_hash,
            change.unified_diff,
            change.warning,
        ),
    )
}

fn direct_plan_result(plan: DirectPlan) -> DirectPlanResult {
    let operations = |operations: Vec<(String, usize, ResourceKind)>| -> Vec<OperationResult> {
        operations
            .into_iter()
            .map(|(name, key, kind)| (name, key, kind.as_str()))
            .collect()
    };
    (
        plan.user_scope,
        plan.execution_scope,
        plan.prerequisite_execution_scope,
        plan.prerequisites
            .into_iter()
            .map(|item| {
                (
                    item.key,
                    item.relation_name,
                    item.present,
                    item.framework_managed,
                )
            })
            .collect(),
        plan.entries.into_iter().map(entry_result).collect(),
        plan.replay_roots
            .into_iter()
            .map(|root| {
                let columns = root.columns;
                (
                    root.key,
                    root.driving_input,
                    root.driving_input_relation_name,
                    [
                        columns.partition,
                        columns.offset,
                        columns.timestamp,
                        columns.landed_at,
                        columns.cursor,
                    ],
                    root.lineage.as_str(),
                    root.propagated,
                    root.aggregate,
                    root.replay_settings,
                )
            })
            .collect(),
        operations(plan.teardown),
        operations(plan.creation),
        plan.mutable_ref_warnings,
    )
}

/// Plan the selected downstream closure over `nodes` in graph order. A violated rule raises
/// `ValueError(message)`.
#[pyfunction]
#[pyo3(signature = (
    *,
    nodes,
    database,
    selection_mode,
    selected,
    source_drift,
    include_missing_upstream,
    has_start_time,
    catalog,
    external_columns,
    fingerprints,
    mutable_ref_names,
))]
#[allow(clippy::too_many_arguments)]
fn plan_direct_build(
    py: Python<'_>,
    nodes: Vec<NodeArgument>,
    database: &str,
    selection_mode: &str,
    selected: HashSet<usize>,
    source_drift: HashSet<usize>,
    include_missing_upstream: bool,
    has_start_time: bool,
    catalog: Vec<CatalogArgument>,
    external_columns: Vec<ExternalArgument>,
    fingerprints: FingerprintArgument,
    mutable_ref_names: Vec<String>,
) -> PyResult<DirectPlanResult> {
    let count = nodes.len();
    if nodes
        .iter()
        .any(|(_, _, _, _, _, _, _, _, upstream, downstream)| {
            upstream.iter().any(|(key, _)| *key >= count)
                || downstream.iter().any(|key| *key >= count)
        })
        || selected
            .iter()
            .chain(&source_drift)
            .any(|key| *key >= count)
    {
        return Err(PyValueError::new_err("Direct plan key index out of range"));
    }
    let (available, warning, baselines) = &fingerprints;
    let input = DirectInput {
        nodes: nodes.iter().map(node).collect::<PyResult<_>>()?,
        database,
        selection_mode: parse_selection_mode(selection_mode)?,
        selected,
        source_drift,
        include_missing_upstream,
        has_start_time,
        catalog: catalog
            .iter()
            .map(
                |(name, materialized_view, columns, sources)| CatalogRelation {
                    name,
                    materialized_view: *materialized_view,
                    columns: columns.iter().map(String::as_str).collect(),
                    source_relation_names: sources.iter().map(String::as_str).collect(),
                },
            )
            .collect(),
        external_columns: external_columns
            .iter()
            .map(
                |(name, [partition, offset, timestamp, landed_at, cursor])| {
                    (
                        name.as_str(),
                        ExternalColumns {
                            partition: partition.as_deref(),
                            offset: offset.as_deref(),
                            timestamp: timestamp.as_deref(),
                            landed_at: landed_at.as_deref(),
                            cursor: cursor.as_deref(),
                        },
                    )
                },
            )
            .collect(),
        fingerprints: if *available {
            Fingerprints::Available(
                baselines
                    .iter()
                    .map(|(identity, sql, hash)| (identity.as_str(), sql.as_str(), hash.as_str()))
                    .collect(),
            )
        } else {
            Fingerprints::Unavailable {
                warning: warning.as_deref(),
            }
        },
        mutable_ref_names: mutable_ref_names.iter().map(String::as_str).collect(),
    };
    py.detach(|| plan_direct(&input))
        .map(direct_plan_result)
        .map_err(PyValueError::new_err)
}

/// `(engine, order by, partition by, TTL, settings)`.
type StorageArgument = (
    String,
    Vec<String>,
    Option<String>,
    Option<String>,
    Option<Vec<(String, String)>>,
);
/// `(model name, kind, relation name, ((column, type), ...), table storage)`.
type IdentityArgument = (
    String,
    String,
    String,
    Vec<(String, String)>,
    Option<StorageArgument>,
);

fn model_identity(argument: IdentityArgument) -> direct_fingerprint::ModelIdentity {
    let (model_name, kind, relation_name, output_columns, storage) = argument;
    direct_fingerprint::ModelIdentity {
        model_name,
        kind,
        relation_name,
        output_columns,
        storage: storage.map(|(engine, order_by, partition_by, ttl, settings)| {
            direct_fingerprint::Storage {
                engine,
                order_by,
                partition_by,
                ttl,
                settings,
            }
        }),
    }
}

/// `(fingerprint ID, logical model identity, definition hash, identity metadata)` of the
/// record a direct build writes for one applied model.
#[pyfunction]
fn direct_fingerprint_record(
    identity: IdentityArgument,
    query: &str,
    database: &str,
    workflow_id: &str,
) -> (String, String, String, String) {
    let record =
        direct_fingerprint::record(&model_identity(identity), query, database, workflow_id);
    (
        record.fingerprint_id,
        record.logical_model_identity,
        record.definition_hash,
        record.identity_metadata,
    )
}

/// The sorted, compact, ASCII-only JSON identity of one model.
#[pyfunction]
fn direct_model_identity_metadata(identity: IdentityArgument) -> String {
    direct_fingerprint::identity_metadata(&model_identity(identity))
}

/// How a model drifted from its `(definition hash, identity metadata)` baseline.
#[pyfunction]
fn direct_model_drift_reasons(
    identity: IdentityArgument,
    query: &str,
    baseline: Option<(String, String)>,
) -> Vec<&'static str> {
    direct_fingerprint::drift_reasons(
        &model_identity(identity),
        query,
        baseline
            .as_ref()
            .map(|(hash, metadata)| (hash.as_str(), metadata.as_str())),
    )
}

/// `(key index, name, kind, materialized view target)` for one desired object.
type ActualObjectArgument = (usize, String, String, Option<String>);
/// `(is reconcile, normalized query)`.
type RecordArgument = (bool, Option<String>);
/// `None`, or `(source, active table or query)` with source `desired`, `active_table`, `query`.
type ActualSourceResult = Option<(&'static str, Option<String>)>;

/// Each desired object's actual source. A missing required lookup raises
/// `ValueError(("root", key index) | ("physical_name", name) | ("table_spec", name))`.
#[pyfunction]
#[allow(clippy::needless_pass_by_value)]
fn actual_state_sources(
    objects: Vec<ActualObjectArgument>,
    existing_names: HashSet<String>,
    roots: HashMap<usize, (bool, Option<String>)>,
    records_by_deployment_and_key: HashMap<(String, usize), RecordArgument>,
    latest_records: HashMap<usize, RecordArgument>,
    active_physical_names: HashMap<String, String>,
    active_table_spec_names: HashSet<String>,
) -> PyResult<Vec<ActualSourceResult>> {
    let facts: Vec<DesiredObjectFacts<'_>> = objects
        .iter()
        .map(|(key, name, kind, target)| {
            let kind: DesiredFacts<'_> = match (kind.as_str(), target) {
                ("kafka_table", _) => DesiredFacts::KafkaTable,
                ("table", _) => DesiredFacts::Table,
                ("materialized_view", Some(target)) => DesiredFacts::MaterializedView {
                    target_table_name: target,
                },
                ("view", _) => DesiredFacts::View,
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown desired object kind '{kind}'"
                    )));
                }
            };
            Ok(DesiredObjectFacts {
                key: *key,
                name,
                kind,
            })
        })
        .collect::<PyResult<_>>()?;
    let inspection = InspectionFacts {
        existing_names: existing_names.iter().map(String::as_str).collect(),
        roots: roots
            .iter()
            .map(|(key, (present, id))| {
                (
                    *key,
                    RootFacts {
                        active_view_present: *present,
                        active_deployment_id: id.as_deref(),
                    },
                )
            })
            .collect(),
        records_by_deployment_and_key: records_by_deployment_and_key
            .iter()
            .map(|((id, key), value)| ((id.as_str(), *key), record_facts(value)))
            .collect(),
        latest_records: latest_records
            .iter()
            .map(|(key, value)| (*key, record_facts(value)))
            .collect(),
        active_physical_names: active_physical_names
            .iter()
            .map(|(logical, physical)| (logical.as_str(), physical.as_str()))
            .collect(),
        active_table_spec_names: active_table_spec_names.iter().map(String::as_str).collect(),
    };
    match actual_sources(&facts, &inspection) {
        Ok(sources) => Ok(sources
            .into_iter()
            .map(|source| {
                source.map(|source| match source {
                    ActualSource::Desired => ("desired", None),
                    ActualSource::ActiveTable(name) => ("active_table", Some(name.to_owned())),
                    ActualSource::Query(query) => ("query", Some(query.to_owned())),
                })
            })
            .collect()),
        Err(ActualStateLookup::Root(key)) => Err(PyValueError::new_err(("root", key))),
        Err(ActualStateLookup::PhysicalName(name)) => {
            Err(PyValueError::new_err(("physical_name", name.to_owned())))
        }
        Err(ActualStateLookup::TableSpec(name)) => {
            Err(PyValueError::new_err(("table_spec", name.to_owned())))
        }
    }
}

/// The position of each wanted key's latest record from `(key index when wanted, recorded at,
/// observation ID)` stamps, ordered by each key's first record.
#[pyfunction]
fn latest_object_state_positions(records: Vec<(Option<usize>, String, String)>) -> Vec<usize> {
    let stamps: Vec<(Option<usize>, &str, &str)> = records
        .iter()
        .map(|(key, recorded_at, observation_id)| {
            (*key, recorded_at.as_str(), observation_id.as_str())
        })
        .collect();
    latest_record_positions(&stamps)
}

/// The logical name of a deployment-suffixed model relation, excluding Kafka tables and
/// materialized views.
#[pyfunction]
fn deployment_candidate_logical_name(physical_name: &str) -> Option<&str> {
    candidate_logical_name(physical_name)
}

fn borrowed(rows: &[(String, String, String)]) -> Vec<(&str, &str, &str)> {
    rows.iter()
        .map(|(database, logical, physical)| {
            (database.as_str(), logical.as_str(), physical.as_str())
        })
        .collect()
}

fn record_facts((is_reconcile, query): &RecordArgument) -> RecordFacts<'_> {
    RecordFacts {
        is_reconcile: *is_reconcile,
        normalized_query: query.as_deref(),
    }
}

/// Stable bindings as `(logical name, physical name)` and deployment candidates as `(logical name,
/// physical name, is view)`.
type ManagedRelationsResult = (Vec<(String, String)>, Vec<(String, String, bool)>);

/// `(stable bindings as (logical name, physical name), deployment candidates as (logical name,
/// physical name, is view))` from `(name, is view, stable binding name)` catalog relations.
#[pyfunction]
fn managed_catalog_relations(
    relations: Vec<(String, bool, Option<String>)>,
) -> ManagedRelationsResult {
    let relations: Vec<Relation<'_>> = relations
        .iter()
        .map(|(name, is_view, stable_binding_name)| Relation {
            name,
            is_view: *is_view,
            stable_binding_name: stable_binding_name.as_deref(),
        })
        .collect();
    let (bindings, candidates) = managed_relations(&relations);
    (
        bindings
            .into_iter()
            .map(|(logical, physical)| (logical.to_owned(), physical.to_owned()))
            .collect(),
        candidates
            .into_iter()
            .map(|(logical, physical, is_view)| (logical.to_owned(), physical.to_owned(), is_view))
            .collect(),
    )
}

/// `(state kind, active deployment ID)` of one managed root from `(database, logical name,
/// physical name)` bindings and candidates.
#[pyfunction]
fn root_deployment_state(
    bindings: Vec<(String, String, String)>,
    candidates: Vec<(String, String, String)>,
    root_database: Option<&str>,
    root_name: &str,
) -> (&'static str, Option<String>) {
    let state = root_state(
        &borrowed(&bindings),
        &borrowed(&candidates),
        root_database,
        root_name,
    );
    let deployment_id: Option<String> = match state {
        RootState::ActiveViewPresent(deployment_id) => Some(deployment_id.to_owned()),
        _ => None,
    };
    (state.kind(), deployment_id)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(actual_state_sources, module)?)?;
    module.add_function(wrap_pyfunction!(managed_catalog_relations, module)?)?;
    module.add_function(wrap_pyfunction!(root_deployment_state, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_candidate_logical_name, module)?)?;
    module.add_function(wrap_pyfunction!(latest_object_state_positions, module)?)?;
    module.add_function(wrap_pyfunction!(direct_fingerprint_record, module)?)?;
    module.add_function(wrap_pyfunction!(direct_model_drift_reasons, module)?)?;
    module.add_function(wrap_pyfunction!(direct_model_identity_metadata, module)?)?;
    module.add_function(wrap_pyfunction!(classify_object_changes, module)?)?;
    module.add_function(wrap_pyfunction!(unified_diff, module)?)?;
    module.add_function(wrap_pyfunction!(stage_deployment, module)?)?;
    module.add_function(wrap_pyfunction!(plan_direct_build, module)?)?;
    module.add_class::<PyDesiredGraph>()
}
