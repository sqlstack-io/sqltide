//! Python bindings that assemble actual state from a captured warehouse snapshot.

use std::collections::HashMap;

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use sqltide_planner::actual::{ActualSource, DesiredFacts};
use sqltide_planner::actual_assembly::{
    AssemblyError, DesiredObject, ObjectKeyRef, StateRecord, assemble_actual_sources,
    normalize_storage_engine,
};
use sqltide_planner::inspection::Relation;

const ADAPTER_CONSTANTS: &str = "sqltide.adapter.constants";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const PLANNER_EXCEPTIONS: &str = "sqltide.compiler.planner.exceptions";
const PLANNER_MODELS: &str = "sqltide.compiler.planner.models";
const RAW_TABLE_NAME_PREFIX: &str = "raw__";

fn keyword_call<'py>(
    callable: &Bound<'py, PyAny>,
    arguments: &[(&str, Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let keywords: Bound<'py, PyDict> = PyDict::new(callable.py());
    for (name, value) in arguments {
        keywords.set_item(name, value)?;
    }
    callable.call((), Some(&keywords))
}

/// An `ObjectKey`'s parts, with its object type as text.
struct OwnedKey {
    database: Option<String>,
    object_type: String,
    name: String,
}

impl OwnedKey {
    fn read(key: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            database: key.getattr("database")?.extract()?,
            object_type: key.getattr("object_type")?.str()?.to_string(),
            name: key.getattr("name")?.extract()?,
        })
    }

    fn borrowed(&self) -> ObjectKeyRef<'_> {
        ObjectKeyRef {
            database: self.database.as_deref(),
            object_type: &self.object_type,
            name: &self.name,
        }
    }
}

enum Kind {
    KafkaTable,
    Table,
    MaterializedView(String),
    View,
}

/// The four desired object classes, in the order an object is classified.
struct DesiredClasses<'py> {
    kafka_table: Bound<'py, PyAny>,
    table: Bound<'py, PyAny>,
    materialized_view: Bound<'py, PyAny>,
}

impl<'py> DesiredClasses<'py> {
    fn import(py: Python<'py>) -> PyResult<Self> {
        let models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
        Ok(Self {
            kafka_table: models.getattr("DesiredKafkaTable")?,
            table: models.getattr("DesiredTable")?,
            materialized_view: models.getattr("DesiredMaterializedView")?,
        })
    }

    fn kind(&self, object: &Bound<'py, PyAny>) -> PyResult<Kind> {
        Ok(if object.is_instance(&self.kafka_table)? {
            Kind::KafkaTable
        } else if object.is_instance(&self.table)? {
            Kind::Table
        } else if object.is_instance(&self.materialized_view)? {
            Kind::MaterializedView(object.getattr("target_table_name")?.extract()?)
        } else {
            Kind::View
        })
    }
}

/// The comparable table spec of one catalog relation.
fn catalog_table_spec<'py>(relation: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = relation.py();
    let models: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    let column_class: Bound<'py, PyAny> = models.getattr("Column")?;
    let columns: Vec<Bound<'py, PyAny>> = relation
        .getattr("columns")?
        .try_iter()?
        .map(|column| {
            let column: Bound<'py, PyAny> = column?;
            keyword_call(
                &column_class,
                &[
                    ("name", column.getattr("name")?),
                    ("type", column.getattr("type")?),
                    ("default", column.getattr("default_expression")?),
                ],
            )
        })
        .collect::<PyResult<_>>()?;
    let engine: String = relation.getattr("storage_engine")?.extract()?;
    let storage: Bound<'py, PyAny> = keyword_call(
        &models.getattr("TableStorage")?,
        &[
            (
                "engine",
                normalize_storage_engine(&engine)
                    .into_pyobject(py)?
                    .into_any(),
            ),
            ("order_by", relation.getattr("order_by")?),
            ("partition_by", relation.getattr("partition_by")?),
            ("ttl", relation.getattr("ttl")?),
            ("settings", py.None().into_bound(py)),
        ],
    )?;
    keyword_call(
        &models.getattr("TableSpec")?,
        &[
            ("columns", PyTuple::new(py, columns)?.into_any()),
            ("storage", storage),
        ],
    )
}

/// The first catalog relation with this name, as `CatalogSnapshot.relation` finds it.
fn catalog_relation<'py>(
    relations: &[Bound<'py, PyAny>],
    names: &[String],
    name: &str,
) -> Option<Bound<'py, PyAny>> {
    names
        .iter()
        .position(|known| known == name)
        .map(|position| relations[position].clone())
}

/// The actual object of one desired object from its assembled source.
fn actual_object<'py>(
    desired: &Bound<'py, PyAny>,
    kind: &Kind,
    source: &ActualSource<'_>,
    relations: &[Bound<'py, PyAny>],
    relation_names: &[String],
    table_specs: &mut HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py: Python<'py> = desired.py();
    let compile: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    let planner: Bound<'py, PyModule> = py.import(PLANNER_MODELS)?;
    let key: Bound<'py, PyAny> = desired.getattr("key")?;
    let (class, spec): (&str, Bound<'py, PyAny>) = match (kind, source) {
        (Kind::KafkaTable, _) => (
            "ActualKafkaTable",
            keyword_call(
                &compile.getattr("KafkaTableSpec")?,
                &[
                    ("columns", desired.getattr("columns")?),
                    ("kafka", desired.getattr("kafka")?),
                ],
            )?,
        ),
        (Kind::Table, ActualSource::ActiveTable(physical)) => {
            let spec: Bound<'py, PyAny> = match table_specs.get(*physical) {
                Some(spec) => spec.clone(),
                None => {
                    let relation: Bound<'py, PyAny> =
                        catalog_relation(relations, relation_names, physical)
                            .expect("assembly checked every active table has a catalog relation");
                    let spec: Bound<'py, PyAny> = catalog_table_spec(&relation)?;
                    table_specs.insert((*physical).to_owned(), spec.clone());
                    spec
                }
            };
            ("ActualTable", spec)
        }
        (Kind::Table, _) => {
            let storage: Bound<'py, PyAny> = keyword_call(
                &compile.getattr("TableStorage")?,
                &[
                    ("engine", desired.getattr("engine")?),
                    ("order_by", desired.getattr("order_by")?),
                    ("partition_by", desired.getattr("partition_by")?),
                    ("ttl", desired.getattr("ttl")?),
                    ("settings", desired.getattr("settings")?),
                ],
            )?;
            (
                "ActualTable",
                keyword_call(
                    &compile.getattr("TableSpec")?,
                    &[
                        ("columns", desired.getattr("columns")?),
                        ("storage", storage),
                    ],
                )?,
            )
        }
        (Kind::MaterializedView(_), source) => {
            let query: Bound<'py, PyAny> = match source {
                ActualSource::Query(query) => query.into_pyobject(py)?.into_any(),
                _ => desired.getattr("query")?,
            };
            (
                "ActualMaterializedView",
                keyword_call(
                    &compile.getattr("MaterializedViewSpec")?,
                    &[
                        ("source_table_name", desired.getattr("source_table_name")?),
                        ("target_table_name", desired.getattr("target_table_name")?),
                        ("query", query.clone()),
                        ("database_template", query),
                    ],
                )?,
            )
        }
        (Kind::View, source) => {
            let query: Bound<'py, PyAny> = match source {
                ActualSource::Query(query) => query.into_pyobject(py)?.into_any(),
                ActualSource::ActiveTable(name) => name.into_pyobject(py)?.into_any(),
                ActualSource::Desired => "None".into_pyobject(py)?.into_any(),
            };
            (
                "ActualView",
                keyword_call(
                    &compile.getattr("ViewSpec")?,
                    &[("query", query.clone()), ("database_template", query)],
                )?,
            )
        }
    };
    keyword_call(&planner.getattr(class)?, &[("key", key), ("spec", spec)])
}

fn actual_state<'py>(
    py: Python<'py>,
    objects: Vec<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    keyword_call(
        &py.import(PLANNER_MODELS)?.getattr("ActualState")?,
        &[("objects", PyTuple::new(py, objects)?.into_any())],
    )
}

/// The actual state of `desired_state` from one captured planning snapshot, without another
/// warehouse read.
#[pyfunction]
#[pyo3(signature = (*, snapshot, desired_state, database))]
fn load_actual_state_from_snapshot<'py>(
    py: Python<'py>,
    snapshot: &Bound<'py, PyAny>,
    desired_state: &Bound<'py, PyAny>,
    database: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let classes: DesiredClasses<'py> = DesiredClasses::import(py)?;
    let desired: Vec<Bound<'py, PyAny>> = desired_state
        .getattr("objects")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let mut desired_keys: Vec<OwnedKey> = Vec::with_capacity(desired.len());
    let mut desired_names: Vec<String> = Vec::with_capacity(desired.len());
    let mut kinds: Vec<Kind> = Vec::with_capacity(desired.len());
    for object in &desired {
        desired_keys.push(OwnedKey::read(&object.getattr("key")?)?);
        desired_names.push(object.getattr("name")?.extract()?);
        kinds.push(classes.kind(object)?);
    }
    let catalog: Bound<'py, PyAny> = snapshot.getattr("catalog")?;
    let catalog_database: String = catalog
        .getattr("identity")?
        .getattr("database")?
        .extract()?;
    let relations: Vec<Bound<'py, PyAny>> = catalog
        .getattr("relations")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let mut relation_names: Vec<String> = Vec::with_capacity(relations.len());
    let mut relation_views: Vec<bool> = Vec::with_capacity(relations.len());
    let mut relation_bindings: Vec<Option<String>> = Vec::with_capacity(relations.len());
    for relation in &relations {
        relation_names.push(relation.getattr("name")?.extract()?);
        relation_views.push(!relation.getattr("query_sql")?.is_none());
        relation_bindings.push(relation.getattr("stable_binding_name")?.extract()?);
    }
    let reconcile_kind: String = py
        .import(ADAPTER_CONSTANTS)?
        .getattr("VIRTUAL_OBJECT_STATE_KIND_RECONCILE")?
        .extract()?;
    let mut record_keys: Vec<OwnedKey> = Vec::new();
    let mut record_texts: Vec<(String, bool, Option<String>, String, String)> = Vec::new();
    for record in snapshot.getattr("object_state_records")?.try_iter()? {
        let record: Bound<'py, PyAny> = record?;
        record_keys.push(OwnedKey::read(&record.getattr("key")?)?);
        let state_kind: Bound<'py, PyAny> = record.getattr("state_kind")?;
        record_texts.push((
            record.getattr("deployment_id")?.extract()?,
            state_kind.eq(&reconcile_kind)?,
            record.getattr("normalized_query")?.extract()?,
            record.getattr("recorded_at")?.extract()?,
            record.getattr("observation_id")?.extract()?,
        ));
    }
    let objects: Vec<DesiredObject<'_>> = desired_keys
        .iter()
        .zip(&desired_names)
        .zip(&kinds)
        .map(|((key, name), kind)| DesiredObject {
            key: key.borrowed(),
            name,
            kind: match kind {
                Kind::KafkaTable => DesiredFacts::KafkaTable,
                Kind::Table => DesiredFacts::Table,
                Kind::MaterializedView(target) => DesiredFacts::MaterializedView {
                    target_table_name: target,
                },
                Kind::View => DesiredFacts::View,
            },
        })
        .collect();
    let catalog_relations: Vec<Relation<'_>> = relation_names
        .iter()
        .zip(&relation_views)
        .zip(&relation_bindings)
        .map(|((name, is_view), binding)| Relation {
            name,
            is_view: *is_view,
            stable_binding_name: binding.as_deref(),
        })
        .collect();
    let records: Vec<StateRecord<'_>> = record_keys
        .iter()
        .zip(&record_texts)
        .map(
            |(key, (deployment_id, is_reconcile, query, recorded_at, observation_id))| {
                StateRecord {
                    deployment_id,
                    key: key.borrowed(),
                    is_reconcile: *is_reconcile,
                    normalized_query: query.as_deref(),
                    recorded_at,
                    observation_id,
                }
            },
        )
        .collect();
    let assembled =
        match assemble_actual_sources(&objects, &catalog_database, &catalog_relations, &records) {
            Ok(assembled) => assembled,
            Err(AssemblyError::MissingKey(missing)) => {
                let position: usize = objects
                    .iter()
                    .position(|object| object.key == missing)
                    .expect("a missing key is one of the desired keys");
                return Err(PyKeyError::new_err(
                    desired[position].getattr("key")?.unbind(),
                ));
            }
            Err(AssemblyError::MissingName(name)) => {
                return Err(PyKeyError::new_err(name.to_owned()));
            }
            Err(AssemblyError::MissingTableMetadata(name)) => {
                let error: Bound<'py, PyAny> = py
                    .import(PLANNER_EXCEPTIONS)?
                    .getattr("ActualStateError")?
                    .call1((format!(
                        "Expected live table metadata for {database}.{name}"
                    ),))?;
                return Err(PyErr::from_value(error));
            }
        };
    let mut table_specs: HashMap<String, Bound<'py, PyAny>> = HashMap::new();
    let actual: Vec<Bound<'py, PyAny>> = assembled
        .iter()
        .map(|(position, source)| {
            actual_object(
                &desired[*position],
                &kinds[*position],
                source,
                &relations,
                &relation_names,
                &mut table_specs,
            )
        })
        .collect::<PyResult<_>>()?;
    actual_state(py, actual)
}

/// Direct-mode actual state: the existing model tables with their catalog specs and the
/// existing materialized views as the catalog records them, in desired order.
#[pyfunction]
#[pyo3(signature = (*, desired_state, catalog))]
fn build_direct_reconcile_actual_state<'py>(
    py: Python<'py>,
    desired_state: &Bound<'py, PyAny>,
    catalog: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let classes: DesiredClasses<'py> = DesiredClasses::import(py)?;
    let compile: Bound<'py, PyModule> = py.import(COMPILE_MODELS)?;
    let planner: Bound<'py, PyModule> = py.import(PLANNER_MODELS)?;
    let relations: Vec<Bound<'py, PyAny>> = catalog
        .getattr("relations")?
        .try_iter()?
        .collect::<PyResult<_>>()?;
    let relation_names: Vec<String> = relations
        .iter()
        .map(|relation| relation.getattr("name")?.extract())
        .collect::<PyResult<_>>()?;
    let mut table_specs: HashMap<String, Bound<'py, PyAny>> = HashMap::new();
    let mut objects: Vec<Bound<'py, PyAny>> = Vec::new();
    for desired in desired_state.getattr("objects")?.try_iter()? {
        let desired: Bound<'py, PyAny> = desired?;
        let name: String = desired.getattr("name")?.extract()?;
        let Some(relation) = catalog_relation(&relations, &relation_names, &name) else {
            continue;
        };
        match classes.kind(&desired)? {
            Kind::Table if !name.starts_with(RAW_TABLE_NAME_PREFIX) => {
                let spec: Bound<'py, PyAny> = match table_specs.get(&name) {
                    Some(spec) => spec.clone(),
                    None => {
                        let spec: Bound<'py, PyAny> = catalog_table_spec(&relation)?;
                        table_specs.insert(name, spec.clone());
                        spec
                    }
                };
                objects.push(keyword_call(
                    &planner.getattr("ActualTable")?,
                    &[("key", desired.getattr("key")?), ("spec", spec)],
                )?);
            }
            Kind::MaterializedView(_) => {
                let or_empty = |value: Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
                    if value.is_truthy()? {
                        Ok(value)
                    } else {
                        Ok("".into_pyobject(py)?.into_any())
                    }
                };
                let spec: Bound<'py, PyAny> = keyword_call(
                    &compile.getattr("MaterializedViewSpec")?,
                    &[
                        (
                            "source_table_name",
                            or_empty(relation.getattr("source_relation_name")?)?,
                        ),
                        (
                            "target_table_name",
                            or_empty(relation.getattr("target_relation_name")?)?,
                        ),
                        ("query", or_empty(relation.getattr("query_sql")?)?),
                    ],
                )?;
                objects.push(keyword_call(
                    &planner.getattr("ActualMaterializedView")?,
                    &[("key", desired.getattr("key")?), ("spec", spec)],
                )?);
            }
            _ => {}
        }
    }
    actual_state(py, objects)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(load_actual_state_from_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(
        build_direct_reconcile_actual_state,
        module
    )?)
}
