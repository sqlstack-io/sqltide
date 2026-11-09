//! Python bindings for deployment planning over desired state.

use std::collections::HashSet;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{IntoPyDict, PyDict, PyTuple};
use sqltide_planner::deployment::{
    MUTABLE_REF_WARNING_CODE, MUTABLE_REF_WARNING_MESSAGE, PlanKey, PlanSubtree,
    stage_deployment as stage,
};
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
    managed_relations, root_state,
};
use sqltide_planner::rebuild::{
    ChangeType, ObjectChange, RebuildSubtree, SchemaChangeKind, SeedCompatibility, rebuild_subtrees,
};

/// `(mode, lookback seconds)`.
type RuleArgument = (String, Option<i64>);
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

const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const COMPILE_CONSTANTS: &str = "sqltide.compiler.compile.constants";
const GRAPH_EXCEPTIONS: &str = "sqltide.compiler.graph.exceptions";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const PLANNER_CONSTANTS: &str = "sqltide.compiler.planner.constants";
const PLANNER_TYPES: &str = "sqltide.compiler.planner.types";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";

/// `GraphInputError` for a dependency cycle, raised `from None` as Python raised it.
fn graph_input_error(py: Python<'_>, error: CycleError) -> PyErr {
    match py
        .import(GRAPH_EXCEPTIONS)
        .and_then(|module| module.getattr("GraphInputError"))
        .and_then(|class| class.call1((error.message,)))
    {
        Ok(exception) => {
            let failure: PyErr = PyErr::from_value(exception);
            failure.set_cause(py, None);
            failure
        }
        Err(failure) => failure,
    }
}

fn text(object: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(object.str()?.to_string())
}

/// `str(value)`, or `None` when `value` is `None`.
fn optional_text(object: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if object.is_none() {
        Ok(None)
    } else {
        text(object).map(Some)
    }
}

/// Desired-state keys numbered in the order the state first names them, with Python key
/// equality.
struct Interner<'py> {
    keys: Vec<Bound<'py, PyAny>>,
    index: Bound<'py, PyDict>,
    infos: Vec<KeyInfo>,
}

impl<'py> Interner<'py> {
    fn intern(&mut self, key: &Bound<'py, PyAny>) -> PyResult<usize> {
        if let Some(found) = self.index.get_item(key)? {
            return found.extract();
        }
        let position: usize = self.keys.len();
        self.index.set_item(key, position)?;
        self.infos.push(KeyInfo {
            object_type: object_type(&text(&key.getattr("object_type")?)?)?,
            name: key.getattr("name")?.extract()?,
        });
        self.keys.push(key.clone());
        Ok(position)
    }
}

/// `(mode, lookback seconds)` of one replay-on-change rule.
fn rule_argument(rule: &Bound<'_, PyAny>) -> PyResult<Option<RuleArgument>> {
    if rule.is_none() {
        return Ok(None);
    }
    Ok(Some((
        text(&rule.getattr("mode")?)?,
        rule.getattr("lookback_seconds")?.extract()?,
    )))
}

/// One desired object's kind; a materialized view names its source table by key.
fn desired_kind<'py>(
    py: Python<'py>,
    interner: &mut Interner<'py>,
    object: &Bound<'py, PyAny>,
) -> PyResult<ObjectKind> {
    let models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    if object.is_instance(&models.getattr("DesiredMaterializedView")?)? {
        let source_key: Bound<'py, PyAny> = models.getattr("ObjectKey")?.call(
            (),
            Some(
                &[
                    ("database", py.None().into_bound(py)),
                    (
                        "object_type",
                        py.import(COMPILE_CONSTANTS)?
                            .getattr("DESIRED_OBJECT_TYPE_TABLE")?,
                    ),
                    ("name", object.getattr("source_table_name")?),
                ]
                .into_py_dict(py)?,
            ),
        )?;
        let source: usize = interner.intern(&source_key)?;
        return object_kind(
            "materialized_view",
            Some(source),
            Some(object.getattr("target_table_name")?.extract()?),
            None,
        );
    }
    if object.is_instance(&models.getattr("DesiredTable")?)? {
        let policy: Bound<'py, PyAny> = object.getattr("replay_on_change")?;
        let policy = if policy.is_none() {
            None
        } else {
            Some((
                rule_argument(&policy.getattr("breaking")?)?,
                rule_argument(&policy.getattr("non_breaking")?)?,
            ))
        };
        return object_kind("table", None, None, policy);
    }
    if object.is_instance(&models.getattr("DesiredKafkaTable")?)? {
        return object_kind("kafka_table", None, None, None);
    }
    object_kind("view", None, None, None)
}

fn planned_change(index: &Bound<'_, PyDict>, change: &Bound<'_, PyAny>) -> PyResult<ObjectChange> {
    let key: Bound<'_, PyAny> = change.getattr("key")?;
    let position: usize = index
        .get_item(&key)?
        .ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))?
        .extract()?;
    object_change((
        position,
        text(&change.getattr("change_type")?)?,
        change.getattr("force_full_refresh")?.extract()?,
        change.getattr("forced_start_time")?.extract()?,
        optional_text(&change.getattr("schema_change_kind")?)?,
        optional_text(&change.getattr("seed_compatibility")?)?,
    ))
}

/// The dependency graph of one realized desired state, answering in its `ObjectKey`s.
#[pyclass(name = "DesiredGraph", module = "sqltide._native", frozen)]
struct PyDesiredGraph {
    graph: DesiredGraph,
    keys: Vec<Py<PyAny>>,
    index: Py<PyDict>,
}

impl PyDesiredGraph {
    fn key<'py>(&self, py: Python<'py>, position: usize) -> Bound<'py, PyAny> {
        self.keys[position].bind(py).clone()
    }

    fn key_tuple<'py>(
        &self,
        py: Python<'py>,
        positions: &[usize],
    ) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(py, positions.iter().map(|position| self.key(py, *position)))
    }

    fn position(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Option<usize>> {
        self.index
            .bind(py)
            .get_item(key)?
            .map(|found| found.extract())
            .transpose()
    }
}

#[pymethods]
impl PyDesiredGraph {
    /// The graph over `desired_state`'s objects, their dependencies, materialized-view source
    /// tables and replay anchors.
    #[new]
    fn new(py: Python<'_>, desired_state: &Bound<'_, PyAny>) -> PyResult<Self> {
        let mut interner = Interner {
            keys: Vec::new(),
            index: PyDict::new(py),
            infos: Vec::new(),
        };
        let mut objects: Vec<DesiredObject> = Vec::new();
        for object in desired_state.getattr("objects")?.try_iter()? {
            let object: Bound<'_, PyAny> = object?;
            let key: usize = interner.intern(&object.getattr("key")?)?;
            let deps: Vec<usize> = object
                .getattr("deps")?
                .try_iter()?
                .map(|dep| interner.intern(&dep?))
                .collect::<PyResult<_>>()?;
            let kind: ObjectKind = desired_kind(py, &mut interner, &object)?;
            objects.push(DesiredObject { key, deps, kind });
        }
        let anchors: Vec<usize> = desired_state
            .getattr("replay_anchor_keys")?
            .try_iter()?
            .map(|key| interner.intern(&key?))
            .collect::<PyResult<_>>()?;
        Ok(Self {
            graph: DesiredGraph::new(interner.infos, objects, &anchors),
            keys: interner.keys.into_iter().map(Bound::unbind).collect(),
            index: interner.index.unbind(),
        })
    }

    /// Downstream object keys by upstream key, for every key with a downstream object.
    fn reverse_deps<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let reverse: Bound<'py, PyDict> = PyDict::new(py);
        for upstream in self.graph.reverse_dep_keys() {
            reverse.set_item(
                self.key(py, upstream),
                self.key_tuple(py, self.graph.reverse_deps(upstream))?,
            )?;
        }
        Ok(reverse)
    }

    /// The included keys this graph knows, in stable dependency order. A cycle raises
    /// `GraphInputError`.
    fn order<'py>(
        &self,
        py: Python<'py>,
        included_keys: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let mut included: HashSet<usize> = HashSet::new();
        for key in included_keys.try_iter()? {
            if let Some(position) = self.position(py, &key?)? {
                included.insert(position);
            }
        }
        let ordered: Vec<usize> = py
            .detach(|| self.graph.order(&included))
            .map_err(|error| graph_input_error(py, error))?;
        self.key_tuple(py, &ordered)
    }

    /// `root_key` and its downstream object keys in dependency order; empty for an unknown key.
    /// A cycle raises `GraphInputError`.
    fn descendants<'py>(
        &self,
        py: Python<'py>,
        root_key: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let Some(root) = self.position(py, root_key)? else {
            return Ok(PyTuple::empty(py));
        };
        let descendants: Vec<usize> = py
            .detach(|| self.graph.descendants(root))
            .map_err(|error| graph_input_error(py, error))?;
        self.key_tuple(py, &descendants)
    }

    /// The nearest eligible table through realized driving dependencies; an unknown key is its
    /// own anchor. A cycle raises `GraphInputError`.
    fn nearest_replay_anchor<'py>(
        &self,
        py: Python<'py>,
        root_key: &Bound<'py, PyAny>,
        allow_root_key: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let Some(root) = self.position(py, root_key)? else {
            return Ok(root_key.clone());
        };
        let name: String = root_key.getattr("name")?.extract()?;
        let anchor: usize = self
            .graph
            .nearest_replay_anchor(root, allow_root_key, &name)
            .map_err(|error| graph_input_error(py, error))?;
        Ok(self.key(py, anchor))
    }

    /// The `RebuildSubtree`s that `object_changes` require, collapsing descendant roots. A
    /// cycle raises `GraphInputError`.
    fn rebuild_subtrees<'py>(
        &self,
        py: Python<'py>,
        object_changes: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let index: &Bound<'py, PyDict> = self.index.bind(py);
        let changes: Vec<ObjectChange> = object_changes
            .try_iter()?
            .map(|change| planned_change(index, &change?))
            .collect::<PyResult<_>>()?;
        let subtrees: Vec<RebuildSubtree> = py
            .detach(|| rebuild_subtrees(&self.graph, &changes))
            .map_err(|error| graph_input_error(py, error))?;
        self.subtree_objects(py, &subtrees)
    }
}

impl PyDesiredGraph {
    /// The `RebuildSubtree` dataclasses for `subtrees`, in their order.
    fn subtree_objects<'py>(
        &self,
        py: Python<'py>,
        subtrees: &[RebuildSubtree],
    ) -> PyResult<Bound<'py, PyTuple>> {
        let subtree_class = py.import(PLANNER_MODELS)?.getattr("RebuildSubtree")?;
        let strategy = py
            .import(PLANNER_CONSTANTS)?
            .getattr("REBUILD_STRATEGY_SHADOW")?;
        let execution_mode = py.import(PLANNER_TYPES)?.getattr("RebuildExecutionMode")?;
        let replay_mode = py.import(DISCOVERY_TYPES)?.getattr("ReplayOnChangeMode")?;
        let built: Vec<Bound<'py, PyAny>> = subtrees
            .iter()
            .map(|subtree| {
                let backfill_mode: Bound<'py, PyAny> = match &subtree.configured_backfill_mode {
                    None => py.None().into_bound(py),
                    Some(mode) => replay_mode.call1((mode.as_str(),))?,
                };
                subtree_class.call(
                    (),
                    Some(
                        &[
                            ("root_key", self.key(py, subtree.root)),
                            (
                                "affected_keys",
                                self.key_tuple(py, &subtree.affected)?.into_any(),
                            ),
                            (
                                "upstream_boundary_key",
                                self.key(py, subtree.upstream_boundary),
                            ),
                            ("strategy", strategy.clone()),
                            (
                                "replay_required",
                                subtree.replay_required.into_bound_py_any(py)?,
                            ),
                            (
                                "execution_mode",
                                execution_mode.call1((subtree.execution_mode.as_str(),))?,
                            ),
                            (
                                "forced_full_refresh",
                                subtree.forced_full_refresh.into_bound_py_any(py)?,
                            ),
                            (
                                "forced_start_time",
                                subtree.forced_start_time.as_deref().into_bound_py_any(py)?,
                            ),
                            ("configured_backfill_mode", backfill_mode),
                            (
                                "execution_lookback_seconds",
                                subtree.execution_lookback_seconds.into_bound_py_any(py)?,
                            ),
                        ]
                        .into_py_dict(py)?,
                    ),
                )
            })
            .collect::<PyResult<_>>()?;
        PyTuple::new(py, built)
    }
}

const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const BUILD_ADAPTER_RESOURCE: &str = "sqltide.compiler.planner.main.build_adapter_resource";

/// `class(message)` from `module`, raised `from None`.
fn raised_from_none(py: Python<'_>, module: &str, class: &str, message: String) -> PyErr {
    match py
        .import(module)
        .and_then(|module| module.getattr(class))
        .and_then(|class| class.call1((message,)))
    {
        Ok(exception) => {
            let failure: PyErr = PyErr::from_value(exception);
            failure.set_cause(py, None);
            failure
        }
        Err(failure) => failure,
    }
}

/// A `ValueError` from spec conversion or classification as `DeploymentPlanError`.
fn deployment_plan_error(py: Python<'_>, error: PyErr) -> PyErr {
    if !error.is_instance_of::<PyValueError>(py) {
        return error;
    }
    match error.value(py).str() {
        Ok(message) => raised_from_none(
            py,
            PLANNER_EXCEPTIONS,
            "DeploymentPlanError",
            message.to_string(),
        ),
        Err(failure) => failure,
    }
}

fn columns_argument(columns: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String, Option<String>)>> {
    columns
        .try_iter()?
        .map(|column| {
            let column: Bound<'_, PyAny> = column?;
            Ok((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
                column.getattr("default")?.extract()?,
            ))
        })
        .collect()
}

fn settings_argument(settings: &Bound<'_, PyAny>) -> PyResult<Option<Vec<(String, String)>>> {
    if settings.is_none() {
        return Ok(None);
    }
    settings
        .call_method0("items")?
        .try_iter()?
        .map(|item| item?.extract())
        .collect::<PyResult<_>>()
        .map(Some)
}

/// The comparison spec of a desired or actual object: a desired materialized view compares by
/// its `comparison_spec`, everything else by its `spec`.
fn comparison_spec_argument(
    models: &Bound<'_, PyModule>,
    object: &Bound<'_, PyAny>,
) -> PyResult<SpecArgument> {
    let spec: Bound<'_, PyAny> =
        if object.is_instance(&models.getattr("DesiredMaterializedView")?)? {
            object.getattr("comparison_spec")?
        } else {
            object.getattr("spec")?
        };
    if spec.is_instance(&models.getattr("TableSpec")?)? {
        let storage: Bound<'_, PyAny> = spec.getattr("storage")?;
        return Ok((
            "table".to_owned(),
            columns_argument(&spec.getattr("columns")?)?,
            vec![
                storage.getattr("engine")?.extract()?,
                storage.getattr("partition_by")?.extract()?,
                storage.getattr("ttl")?.extract()?,
            ],
            storage.getattr("order_by")?.extract()?,
            settings_argument(&storage.getattr("settings")?)?,
        ));
    }
    if spec.is_instance(&models.getattr("KafkaTableSpec")?)? {
        let kafka: Bound<'_, PyAny> = spec.getattr("kafka")?;
        return Ok((
            "kafka_table".to_owned(),
            columns_argument(&spec.getattr("columns")?)?,
            vec![
                kafka.getattr("broker_list")?.extract()?,
                kafka.getattr("topic")?.extract()?,
                kafka.getattr("consumer_group")?.extract()?,
                kafka.getattr("format")?.extract()?,
            ],
            Vec::new(),
            settings_argument(&kafka.getattr("settings")?)?,
        ));
    }
    if spec.is_instance(&models.getattr("MaterializedViewSpec")?)? {
        return Ok((
            "materialized_view".to_owned(),
            Vec::new(),
            vec![
                spec.getattr("source_table_name")?.extract()?,
                spec.getattr("target_table_name")?.extract()?,
                spec.getattr("query")?.extract()?,
            ],
            Vec::new(),
            None,
        ));
    }
    Ok((
        "view".to_owned(),
        Vec::new(),
        vec![spec.getattr("query")?.extract()?],
        Vec::new(),
        None,
    ))
}

fn keyword_call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = callable.py();
    let keywords: Bound<'py, PyDict> = PyDict::new(py);
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

/// `render_resource` over the adapter resource of `object`, in its key's database or
/// `default_database`.
fn rendered_sql(
    render_resource: &Bound<'_, PyAny>,
    build_resource: &Bound<'_, PyAny>,
    object: &Bound<'_, PyAny>,
    default_database: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let database: Bound<'_, PyAny> = object.getattr("key")?.getattr("database")?;
    let database: Bound<'_, PyAny> = if database.is_truthy()? {
        database
    } else {
        default_database.clone()
    };
    keyword_call(
        render_resource,
        &[
            ("resource", build_resource.call1((object,))?),
            ("database", database),
        ],
    )?
    .extract()
}

/// The desired object an actual object renders as.
fn actual_as_desired<'py>(
    compile_models: &Bound<'py, PyModule>,
    planner_models: &Bound<'py, PyModule>,
    actual: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let class: &str = if actual.is_instance(&planner_models.getattr("ActualKafkaTable")?)? {
        "DesiredKafkaTable"
    } else if actual.is_instance(&planner_models.getattr("ActualTable")?)? {
        "DesiredTable"
    } else if actual.is_instance(&planner_models.getattr("ActualMaterializedView")?)? {
        "DesiredMaterializedView"
    } else {
        "DesiredView"
    };
    keyword_call(
        &compile_models.getattr(class)?,
        &[
            ("key", actual.getattr("key")?),
            ("deps", PyTuple::empty(actual.py()).into_any()),
            ("spec", actual.getattr("spec")?),
        ],
    )
}

/// How a SQL diff names the kind of a desired object.
fn display_object_type(
    compile_models: &Bound<'_, PyModule>,
    object: &Bound<'_, PyAny>,
) -> PyResult<&'static str> {
    let materialized_view: bool =
        object.is_instance(&compile_models.getattr("DesiredMaterializedView")?)?;
    if materialized_view {
        let target: String = object.getattr("target_table_name")?.extract()?;
        if !target.starts_with(sqltide_compile::relations::RAW_TABLE_NAME_PREFIX) {
            return Ok("transform");
        }
    }
    Ok(
        if object.is_instance(&compile_models.getattr("DesiredKafkaTable")?)? {
            "kafka table"
        } else if object.is_instance(&compile_models.getattr("DesiredTable")?)? {
            "table"
        } else if materialized_view {
            "materialized view"
        } else {
            "view"
        },
    )
}

/// A conservative staged `DeploymentPlan` from desired and actual state: classify every
/// desired object against its actual object, collapse the changes into rebuild subtrees, stage
/// them, and diff the rendered SQL of every changed object. A spec that cannot be compared
/// raises `DeploymentPlanError`; a dependency cycle raises `GraphInputError`.
#[pyfunction]
#[pyo3(signature = (
    *,
    desired_state,
    actual_state,
    default_database,
    render_resource,
    deployment_id,
    full_refresh_keys,
    start_time_keys,
    start_time,
))]
#[allow(clippy::too_many_arguments)]
fn plan_deployment<'py>(
    py: Python<'py>,
    desired_state: &Bound<'py, PyAny>,
    actual_state: &Bound<'py, PyAny>,
    default_database: &Bound<'py, PyAny>,
    render_resource: &Bound<'py, PyAny>,
    deployment_id: Option<String>,
    full_refresh_keys: &Bound<'py, PyAny>,
    start_time_keys: &Bound<'py, PyAny>,
    start_time: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let compile_models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    let planner_models: Bound<'py, PyModule> = py.import(PLANNER_MODELS)?;
    let materialized_view_class = compile_models.getattr("DesiredMaterializedView")?;
    let desired_objects: Vec<Bound<'py, PyAny>> = desired_state
        .getattr("objects")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let actual_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in actual_state.getattr("objects")?.try_iter()? {
        let object: Bound<'py, PyAny> = object?;
        actual_by_key.set_item(object.getattr("key")?, object)?;
    }

    // `(key, force full refresh, forced start time)` per desired object.
    let mut forcing: Vec<(Bound<'py, PyAny>, bool, Option<String>)> = Vec::new();
    let mut comparisons: Vec<(SpecArgument, Option<SpecArgument>, bool)> = Vec::new();
    for object in &desired_objects {
        let key: Bound<'py, PyAny> = object.getattr("key")?;
        let forced_start_time: Option<String> = if start_time_keys.contains(&key)? {
            start_time.clone()
        } else {
            None
        };
        let force_full_refresh: bool = full_refresh_keys.contains(&key)?;
        // A dead-letter view compares as unchanged: only its model's rebuild moves it.
        let actual: Option<Bound<'py, PyAny>> = if object.is_instance(&materialized_view_class)?
            && object.getattr("dead_letters")?.is_truthy()?
        {
            Some(object.clone())
        } else {
            actual_by_key.get_item(&key)?
        };
        comparisons.push((
            comparison_spec_argument(&compile_models, object)?,
            actual
                .map(|actual| comparison_spec_argument(&compile_models, &actual))
                .transpose()?,
            force_full_refresh || forced_start_time.is_some(),
        ));
        forcing.push((key, force_full_refresh, forced_start_time));
    }
    let comparisons: Vec<Comparison> = comparisons
        .into_iter()
        .map(|(desired, actual, forced)| {
            Ok(Comparison {
                desired: spec(desired)?,
                actual: actual.map(spec).transpose()?,
                forced,
            })
        })
        .collect::<PyResult<_>>()
        .map_err(|error| deployment_plan_error(py, error))?;
    let classifications: Vec<Classification> = py
        .detach(|| Differ::new().classify_all(&comparisons))
        .map_err(|error| {
            raised_from_none(py, PLANNER_EXCEPTIONS, "DeploymentPlanError", error.message)
        })?;

    let change_class = planner_models.getattr("PlannedObjectChange")?;
    let mut object_changes: Vec<Bound<'py, PyAny>> = Vec::new();
    for ((key, force_full_refresh, forced_start_time), classification) in
        forcing.iter().zip(&classifications)
    {
        let (change_type, schema_change_kind, seed_compatibility) =
            classification_result(*classification);
        object_changes.push(keyword_call(
            &change_class,
            &[
                ("key", key.clone()),
                ("change_type", change_type.into_bound_py_any(py)?),
                (
                    "force_full_refresh",
                    force_full_refresh.into_bound_py_any(py)?,
                ),
                (
                    "forced_start_time",
                    forced_start_time.as_deref().into_bound_py_any(py)?,
                ),
                (
                    "schema_change_kind",
                    schema_change_kind.into_bound_py_any(py)?,
                ),
                (
                    "seed_compatibility",
                    seed_compatibility.into_bound_py_any(py)?,
                ),
            ],
        )?);
    }

    let graph: PyDesiredGraph = PyDesiredGraph::new(py, desired_state)?;
    let changes: Vec<ObjectChange> = forcing
        .iter()
        .zip(&classifications)
        .map(
            |((key, force_full_refresh, forced_start_time), classification)| {
                let (change_type, schema_change_kind, seed_compatibility) =
                    classification_result(*classification);
                let position: usize = graph
                    .position(py, key)?
                    .ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))?;
                object_change((
                    position,
                    change_type.to_owned(),
                    *force_full_refresh,
                    forced_start_time.clone(),
                    schema_change_kind.map(str::to_owned),
                    seed_compatibility.map(str::to_owned),
                ))
            },
        )
        .collect::<PyResult<_>>()?;
    let subtrees: Vec<RebuildSubtree> = py
        .detach(|| rebuild_subtrees(&graph.graph, &changes))
        .map_err(|error| graph_input_error(py, error))?;
    let rebuild_subtree_objects: Bound<'py, PyTuple> = graph.subtree_objects(py, &subtrees)?;

    let mutable_ref_keys: Bound<'py, PyAny> = desired_state.getattr("mutable_ref_warning_keys")?;
    let key_facts: Vec<(String, String, bool)> = graph
        .keys
        .iter()
        .map(|key| {
            let key: &Bound<'py, PyAny> = key.bind(py);
            Ok((
                text(&key.getattr("object_type")?)?,
                key.getattr("name")?.extract()?,
                mutable_ref_keys.contains(key)?,
            ))
        })
        .collect::<PyResult<_>>()?;
    let plan_keys: Vec<PlanKey<'_>> = key_facts
        .iter()
        .map(|(object_type, name, uses_mutable_refs)| PlanKey {
            object_type,
            name,
            uses_mutable_refs: *uses_mutable_refs,
        })
        .collect();
    let plan_subtrees: Vec<PlanSubtree> = subtrees
        .iter()
        .map(|subtree| PlanSubtree {
            root: subtree.root,
            affected: subtree.affected.clone(),
        })
        .collect();
    let staged = stage(&plan_keys, &plan_subtrees, deployment_id.as_deref());

    let step_class = planner_models.getattr("DeploymentStep")?;
    let steps: Vec<Bound<'py, PyAny>> = staged
        .steps
        .into_iter()
        .map(|step| {
            keyword_call(
                &step_class,
                &[
                    ("step_id", step.step_id.into_bound_py_any(py)?),
                    ("phase", step.phase.into_bound_py_any(py)?),
                    ("action", step.action.into_bound_py_any(py)?),
                    ("root_key", graph.key(py, step.root)),
                    (
                        "target_key",
                        match step.target {
                            None => py.None().into_bound(py),
                            Some(target) => graph.key(py, target),
                        },
                    ),
                    ("physical_name", step.physical_name.into_bound_py_any(py)?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;

    let model_kinds = (
        compile_models.getattr("DesiredTable")?,
        materialized_view_class.clone(),
        compile_models.getattr("DesiredView")?,
    );
    let logical_model_name_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in &desired_objects {
        if object.is_instance(&model_kinds.0)?
            || object.is_instance(&model_kinds.1)?
            || object.is_instance(&model_kinds.2)?
        {
            let name: Bound<'py, PyAny> = object.getattr("logical_model_name")?;
            if !name.is_none() {
                logical_model_name_by_key.set_item(object.getattr("key")?, name)?;
            }
        }
    }
    let shadow_class = planner_models.getattr("PreparedShadowObject")?;
    let prepared_shadow_objects: Vec<Bound<'py, PyAny>> = staged
        .shadow_objects
        .into_iter()
        .map(|(position, physical_name)| {
            let key: Bound<'py, PyAny> = graph.key(py, position);
            let logical_model_name: Bound<'py, PyAny> =
                logical_model_name_by_key
                    .get_item(&key)?
                    .ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))?;
            keyword_call(
                &shadow_class,
                &[
                    ("logical_key", key),
                    ("physical_name", physical_name.into_bound_py_any(py)?),
                    ("logical_model_name", logical_model_name),
                ],
            )
        })
        .collect::<PyResult<_>>()?;

    let warning_code = MUTABLE_REF_WARNING_CODE.into_bound_py_any(py)?;
    let warning_message = MUTABLE_REF_WARNING_MESSAGE.into_bound_py_any(py)?;
    let warning_class = planner_models.getattr("PlannerWarning")?;
    let warnings: Vec<Bound<'py, PyAny>> = staged
        .mutable_ref_warnings
        .into_iter()
        .map(|(root, target)| {
            keyword_call(
                &warning_class,
                &[
                    ("warning_code", warning_code.clone()),
                    ("message", warning_message.clone()),
                    ("root_key", graph.key(py, root)),
                    ("target_key", graph.key(py, target)),
                ],
            )
        })
        .collect::<PyResult<_>>()?;

    let desired_by_key: Bound<'py, PyDict> = PyDict::new(py);
    for object in &desired_objects {
        desired_by_key.set_item(object.getattr("key")?, object)?;
    }
    let build_resource = py
        .import(BUILD_ADAPTER_RESOURCE)?
        .getattr("build_adapter_resource")?;
    let diff_class = planner_models.getattr("PlannedSqlDiff")?;
    let mut sql_diffs: Vec<Bound<'py, PyAny>> = Vec::new();
    for ((key, _, _), classification) in forcing.iter().zip(&classifications) {
        if classification.change_type == ChangeType::NoOp {
            continue;
        }
        let Some(desired) = desired_by_key.get_item(key)? else {
            continue;
        };
        let desired_sql: String =
            rendered_sql(render_resource, &build_resource, &desired, default_database)?;
        let current_sql: String = match actual_by_key.get_item(key)? {
            Some(actual) if classification.change_type != ChangeType::Create => rendered_sql(
                render_resource,
                &build_resource,
                &actual_as_desired(&compile_models, &planner_models, &actual)?,
                default_database,
            )?,
            _ => String::new(),
        };
        let diff_lines: Vec<String> = sqltide_planner::unified_diff::unified_diff(
            &split_lines(&current_sql, false),
            &split_lines(&desired_sql, false),
            "current",
            "desired",
            "",
        );
        sql_diffs.push(keyword_call(
            &diff_class,
            &[
                ("key", key.clone()),
                (
                    "object_type",
                    display_object_type(&compile_models, &desired)?.into_bound_py_any(py)?,
                ),
                ("name", desired.getattr("name")?),
                ("diff_lines", PyTuple::new(py, diff_lines)?.into_any()),
            ],
        )?);
    }

    keyword_call(
        &planner_models.getattr("DeploymentPlan")?,
        &[
            ("deployment_id", deployment_id.into_bound_py_any(py)?),
            (
                "object_changes",
                PyTuple::new(py, object_changes)?.into_any(),
            ),
            ("rebuild_subtrees", rebuild_subtree_objects.into_any()),
            ("steps", PyTuple::new(py, steps)?.into_any()),
            (
                "prepared_shadow_objects",
                PyTuple::new(py, prepared_shadow_objects)?.into_any(),
            ),
            ("warnings", PyTuple::new(py, warnings)?.into_any()),
            ("sql_diffs", PyTuple::new(py, sql_diffs)?.into_any()),
        ],
    )
}

/// `(table, aggregate, query, replay settings)` for a model.
pub(crate) type ModelArgument = (bool, bool, String, Vec<(String, String)>);
/// `(name, model, source is framework managed, relation name, [(resource name, kind)], replay
/// lineage, shares a postgres source name, producing pipeline, [(upstream, driving)],
/// downstream)`.
pub(crate) type NodeArgument = (
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
pub(crate) type CatalogArgument = (String, bool, Vec<String>, Vec<String>);
/// `(relation name, [partition, offset, timestamp, landed at, cursor] column overrides)`.
pub(crate) type ExternalArgument = (String, [Option<String>; 5]);
/// `(available, warning, [(identity, definition SQL, definition hash)])`.
pub(crate) type FingerprintArgument = (bool, Option<String>, Vec<(String, String, String)>);
/// `(status, current hash, previous SQL, previous hash, unified diff, warning)`.
pub(crate) type SqlChangeResult = (
    &'static str,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);
/// `(key, reason, [(relation name, kind)], driving input, is replay root, SQL change)`.
pub(crate) type EntryResult = (
    usize,
    &'static str,
    Vec<(String, &'static str)>,
    Option<usize>,
    bool,
    SqlChangeResult,
);
/// `(key, driving input, driving relation, [partition, offset, timestamp, landed at, cursor],
/// lineage, propagated keys, aggregate, replay settings)`.
pub(crate) type ReplayRootResult = (
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
pub(crate) type OperationResult = (String, usize, &'static str);
/// `(user scope, execution scope, prerequisite execution scope, [(key, relation, present,
/// framework managed)], entries, replay roots, teardown, creation, mutable-ref warning indexes)`.
pub(crate) type DirectPlanResult = (
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
        Some("dead_letter_view") => Some(ResourceKind::DeadLetterView),
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
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_direct_plan(
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
    module.add_function(wrap_pyfunction!(managed_catalog_relations, module)?)?;
    module.add_function(wrap_pyfunction!(root_deployment_state, module)?)?;
    module.add_function(wrap_pyfunction!(deployment_candidate_logical_name, module)?)?;
    module.add_function(wrap_pyfunction!(direct_fingerprint_record, module)?)?;
    module.add_function(wrap_pyfunction!(direct_model_drift_reasons, module)?)?;
    module.add_function(wrap_pyfunction!(direct_model_identity_metadata, module)?)?;
    module.add_function(wrap_pyfunction!(plan_deployment, module)?)?;
    module.add_class::<PyDesiredGraph>()
}
