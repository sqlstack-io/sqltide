//! Managed source preparation over the executor's Python shapes: which preserved Kafka sources a
//! direct build keeps, creates or recreates, and which deployed sources drifted. The decisions
//! are the native source planner's; this module reads the realized project and catalog and
//! builds the population shapes.

use pyo3::PyTypeInfo;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use sqltide_executor::sources::{
    DriftedSource, Pipeline, Plan, Preparation, Project, Recreation, Relation, Resource,
    ResourceRef, drifted_sources, plan_preserved,
};

use crate::model_sql::chained;

const ADAPTER_MODELS: &str = "sqltide.adapter.models";
const COMPILE_MODELS: &str = "sqltide.compiler.compile.models";
const DISCOVERY_TYPES: &str = "sqltide.compiler.discovery.types";
const DIRECT_MODELS: &str = "sqltide.executor.direct.models";
const DIRECT_EXCEPTIONS: &str = "sqltide.executor.direct.exceptions";
const POPULATION_MODELS: &str = "sqltide.executor.population.models";

type Value<'py> = Bound<'py, PyAny>;

fn class<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Value<'py>> {
    py.import(module)?.getattr(name)
}

fn is_a(value: &Value<'_>, module: &str, name: &str) -> PyResult<bool> {
    value.is_instance(&class(value.py(), module, name)?)
}

fn items<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn build<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    keywords: &[(&str, Value<'py>)],
) -> PyResult<Value<'py>> {
    let arguments: Bound<'py, PyDict> = PyDict::new(py);
    for (keyword, value) in keywords {
        arguments.set_item(keyword, value)?;
    }
    class(py, module, name)?.call((), Some(&arguments))
}

fn tuple<'py>(py: Python<'py>, values: Vec<Value<'py>>) -> PyResult<Value<'py>> {
    Ok(PyTuple::new(py, values)?.into_any())
}

/// `raise DirectBuildError(message) from None` while handling the native refusal
/// `ValueError(*refusal)`, as the Python translation of a native refusal raised it.
pub(crate) fn direct_build_error<'py>(py: Python<'py>, message: String, refusal: &[&str]) -> PyErr {
    let raised = || -> PyResult<PyErr> {
        let error: Value<'py> =
            class(py, DIRECT_EXCEPTIONS, "DirectBuildError")?.call1((message,))?;
        let context: Value<'py> =
            PyValueError::type_object(py).call1(PyTuple::new(py, refusal)?)?;
        chained(&error, &context, None)?;
        Ok(PyErr::from_value(error))
    };
    raised().unwrap_or_else(|error| error)
}

/// `raise DirectBuildError(message)` with no exception being handled.
pub(crate) fn plain_direct_build_error(py: Python<'_>, message: String) -> PyErr {
    match class(py, DIRECT_EXCEPTIONS, "DirectBuildError").and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Logical keys numbered in first-seen order, like the planner's positional key index.
struct KeyIndex<'py> {
    position_by_key: Bound<'py, PyDict>,
    keys: Vec<Value<'py>>,
}

impl<'py> KeyIndex<'py> {
    fn new(py: Python<'py>) -> Self {
        Self {
            position_by_key: PyDict::new(py),
            keys: Vec::new(),
        }
    }

    fn position(&mut self, key: Value<'py>) -> PyResult<usize> {
        if let Some(position) = self.position_by_key.get_item(&key)? {
            return position.extract();
        }
        let position: usize = self.keys.len();
        self.position_by_key.set_item(&key, position)?;
        self.keys.push(key);
        Ok(position)
    }
}

fn pipeline<'py>(
    py: Python<'py>,
    compiled: &Value<'py>,
    index: &mut KeyIndex<'py>,
) -> PyResult<Pipeline> {
    let declared: Value<'py> = compiled.getattr("pipeline")?;
    let name: Value<'py> = declared.getattr("name")?;
    let mode_class: Value<'py> = class(py, DISCOVERY_TYPES, "PipelineMode")?;
    let direct: bool = mode_class
        .call1((declared.getattr("mode")?,))?
        .eq(mode_class.getattr("DIRECT")?)?;
    let source: Value<'py> = compiled.getattr("source")?;
    let source: Option<usize> = if source.is_none() {
        None
    } else {
        Some(index.position(source.getattr("key")?)?)
    };
    let model_objects: Vec<Value<'py>> = items(&compiled.getattr("models")?)?;
    let mut models: Vec<usize> = Vec::with_capacity(model_objects.len());
    for model in &model_objects {
        models.push(index.position(model.getattr("key")?)?);
    }
    let mut table_transform_sources: Vec<String> = Vec::new();
    for model in &model_objects {
        if is_a(model, COMPILE_MODELS, "CompiledTableModel")? {
            table_transform_sources.push(model.getattr("transform")?.getattr("source")?.extract()?);
        }
    }
    Ok(Pipeline {
        name: name.extract()?,
        quoted_name: name.repr()?.extract()?,
        direct,
        source,
        models,
        table_transform_sources,
    })
}

fn resource(value: &Value<'_>) -> PyResult<Resource> {
    let name = || -> PyResult<String> { value.getattr("name")?.extract() };
    let string = |attribute: &str| -> PyResult<String> { value.getattr(attribute)?.extract() };
    if is_a(value, ADAPTER_MODELS, "AdapterManagedSource")? {
        return Ok(Resource::ManagedSource {
            name: name()?,
            broker_list: string("broker_list")?,
            topic: string("topic")?,
            format: string("format")?,
            consumer_group: string("consumer_group")?,
            settings: value.getattr("settings")?.extract()?,
        });
    }
    if is_a(value, ADAPTER_MODELS, "AdapterTable")? {
        let mut columns: Vec<(String, String)> = Vec::new();
        for column in items(&value.getattr("columns")?)? {
            columns.push((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
            ));
        }
        return Ok(Resource::Table {
            name: name()?,
            columns,
        });
    }
    if is_a(value, ADAPTER_MODELS, "AdapterMaterializedView")? {
        return Ok(Resource::MaterializedView {
            name: name()?,
            source_relation_name: string("source_relation_name")?,
            target_relation_name: string("target_relation_name")?,
        });
    }
    Ok(Resource::Other)
}

/// The realized project with its keys numbered after any keys `index` already holds.
fn project<'py>(
    py: Python<'py>,
    realized_project: &Value<'py>,
    index: &mut KeyIndex<'py>,
) -> PyResult<Project> {
    let compiled: Value<'py> = realized_project.getattr("project")?;
    let mut pipelines: Vec<Pipeline> = Vec::new();
    for compiled_pipeline in items(&compiled.getattr("pipelines")?)? {
        pipelines.push(pipeline(py, &compiled_pipeline, index)?);
    }
    let mut sources: Vec<usize> = Vec::new();
    for source in items(&compiled.getattr("sources")?)? {
        sources.push(index.position(source.getattr("key")?)?);
    }
    let resources_by_key: Value<'py> = realized_project.getattr("resources_by_logical_key")?;
    let empty: Value<'py> = PyTuple::empty(py).into_any();
    let mut resources: Vec<Vec<Resource>> = Vec::with_capacity(index.keys.len());
    let mut key_names: Vec<String> = Vec::with_capacity(index.keys.len());
    for key in &index.keys {
        let key_resources: Value<'py> = resources_by_key.call_method1("get", (key, &empty))?;
        resources.push(
            items(&key_resources)?
                .iter()
                .map(resource)
                .collect::<PyResult<_>>()?,
        );
        key_names.push(key.getattr("name")?.extract()?);
    }
    Ok(Project {
        key_names,
        pipelines,
        sources,
        resources,
    })
}

fn relations(catalog: &Value<'_>) -> PyResult<Vec<Relation>> {
    let mut relations: Vec<Relation> = Vec::new();
    for relation in items(&catalog.getattr("relations")?)? {
        let mut columns: Vec<(String, String)> = Vec::new();
        for column in items(&relation.getattr("columns")?)? {
            columns.push((
                column.getattr("name")?.extract()?,
                column.getattr("type")?.extract()?,
            ));
        }
        relations.push(Relation {
            name: relation.getattr("name")?.extract()?,
            settings: relation.getattr("settings")?.extract()?,
            columns,
            target_relation_name: relation.getattr("target_relation_name")?.extract()?,
        });
    }
    Ok(relations)
}

/// Validate preserved sources and plan absent or recreated resources without mutation, as
/// `(PopulationSourcePreparation, (PopulationRealization, ...))`.
#[pyfunction]
#[pyo3(signature = (*, plan, realized_project, catalog, database))]
pub(crate) fn plan_preserved_managed_sources<'py>(
    py: Python<'py>,
    plan: &Value<'py>,
    realized_project: &Value<'py>,
    catalog: &Value<'py>,
    database: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    let mut execution: Vec<usize> = Vec::new();
    for key in items(&plan.getattr("execution_scope")?)? {
        execution.push(index.position(key)?);
    }
    let mut prerequisites: Vec<usize> = Vec::new();
    for item in items(&plan.getattr("prerequisite_scope")?)? {
        prerequisites.push(index.position(item.getattr("key")?)?);
    }
    let mut drift_selections: Vec<(String, usize)> = Vec::new();
    for selection in items(&plan.getattr("source_drift_selections")?)? {
        drift_selections.push((
            selection.getattr("pipeline_name")?.extract()?,
            index.position(selection.getattr("source_key")?)?,
        ));
    }
    let native_project: Project = project(py, realized_project, &mut index)?;
    let Preparation {
        preserved_relation_names,
        created_relation_names,
        landing_view_names,
        managed_sources,
        recreations,
        realizations,
    } = plan_preserved(
        &Plan {
            execution,
            prerequisites,
            drift_selections,
        },
        &native_project,
        &relations(catalog)?,
        &database.extract::<String>()?,
    )
    .map_err(|message| direct_build_error(py, message.clone(), &[&message]))?;
    let resources_by_key: Value<'py> = realized_project.getattr("resources_by_logical_key")?;
    let resource_at = |reference: &ResourceRef| -> PyResult<Value<'py>> {
        resources_by_key
            .get_item(&index.keys[reference.0])?
            .get_item(reference.1)
    };
    let managed_source = |resource: Value<'py>| -> PyResult<Value<'py>> {
        build(
            py,
            POPULATION_MODELS,
            "PopulationManagedSource",
            &[("resource", resource), ("database", database.clone())],
        )
    };
    let mut recreated: Vec<Value<'py>> = Vec::new();
    for Recreation {
        managed_source: source_reference,
        landing_view,
        consumer_group,
        drifted_setting_names,
    } in &recreations
    {
        let source: Value<'py> = resource_at(source_reference)?;
        let view: Value<'py> = resource_at(landing_view)?;
        if !is_a(&source, ADAPTER_MODELS, "AdapterManagedSource")?
            || !is_a(&view, ADAPTER_MODELS, "AdapterMaterializedView")?
        {
            return Err(plain_direct_build_error(
                py,
                "Managed source recreation resolved unexpected resources".to_owned(),
            ));
        }
        recreated.push(build(
            py,
            POPULATION_MODELS,
            "PopulationSourceRecreation",
            &[
                ("managed_source", managed_source(source)?),
                ("landing_view_name", view.getattr("name")?),
                (
                    "landing_relation_name",
                    view.getattr("target_relation_name")?,
                ),
                (
                    "consumer_group",
                    consumer_group.into_pyobject(py)?.into_any(),
                ),
                (
                    "drifted_setting_names",
                    PyTuple::new(py, drifted_setting_names)?.into_any(),
                ),
            ],
        )?);
    }
    let mut landing_views: Vec<Value<'py>> = Vec::new();
    for desired in items(
        &realized_project
            .getattr("desired_state")?
            .getattr("objects")?,
    )? {
        if is_a(&desired, COMPILE_MODELS, "DesiredMaterializedView")?
            && landing_view_names.contains(&desired.getattr("name")?.extract::<String>()?)
        {
            landing_views.push(desired);
        }
    }
    let mut managed: Vec<Value<'py>> = Vec::new();
    for reference in &managed_sources {
        let resource: Value<'py> = resource_at(reference)?;
        if !is_a(&resource, ADAPTER_MODELS, "AdapterManagedSource")? {
            return Err(plain_direct_build_error(
                py,
                format!(
                    "Resource '{}' is not a managed source",
                    resource.getattr("name")?.str()?
                ),
            ));
        }
        managed.push(managed_source(resource)?);
    }
    let preparation: Value<'py> = build(
        py,
        POPULATION_MODELS,
        "PopulationSourcePreparation",
        &[
            (
                "preserved_relation_names",
                PyTuple::new(py, preserved_relation_names)?.into_any(),
            ),
            (
                "created_relation_names",
                PyTuple::new(py, created_relation_names)?.into_any(),
            ),
            ("landing_views", tuple(py, landing_views)?),
            ("managed_sources", tuple(py, managed)?),
            ("recreated_sources", tuple(py, recreated)?),
        ],
    )?;
    let mut realized: Vec<Value<'py>> = Vec::new();
    for reference in &realizations {
        let resource: Value<'py> = resource_at(reference)?;
        if !is_a(&resource, ADAPTER_MODELS, "AdapterManagedSource")?
            && !is_a(&resource, ADAPTER_MODELS, "AdapterTable")?
        {
            return Err(plain_direct_build_error(
                py,
                format!(
                    "Resource '{}' cannot be realized directly",
                    resource.getattr("name")?.str()?
                ),
            ));
        }
        realized.push(build(
            py,
            POPULATION_MODELS,
            "PopulationRealization",
            &[("resource", resource), ("database", database.clone())],
        )?);
    }
    tuple(py, vec![preparation, tuple(py, realized)?])
}

/// Each deployed managed source whose settings drifted, with its sorted owners, ordered by
/// source name.
#[pyfunction]
#[pyo3(signature = (*, realized_project, catalog))]
fn drifted_managed_sources<'py>(
    py: Python<'py>,
    realized_project: &Value<'py>,
    catalog: &Value<'py>,
) -> PyResult<Value<'py>> {
    let mut index: KeyIndex<'py> = KeyIndex::new(py);
    let native_project: Project = project(py, realized_project, &mut index)?;
    let mut drifted: Vec<Value<'py>> = Vec::new();
    for DriftedSource {
        source,
        owner_pipeline_names,
        drifted_setting_names,
    } in drifted_sources(&native_project, &relations(catalog)?)
    {
        drifted.push(build(
            py,
            DIRECT_MODELS,
            "DriftedManagedSource",
            &[
                ("source_key", index.keys[source].clone()),
                (
                    "owner_pipeline_names",
                    PyTuple::new(py, owner_pipeline_names)?.into_any(),
                ),
                (
                    "drifted_setting_names",
                    PyTuple::new(py, drifted_setting_names)?.into_any(),
                ),
            ],
        )?);
    }
    tuple(py, drifted)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_preserved_managed_sources, module)?)?;
    module.add_function(wrap_pyfunction!(drifted_managed_sources, module)?)
}
