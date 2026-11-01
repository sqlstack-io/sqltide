//! Python bindings for managed source preparation. Refusals raise `ValueError(message)`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqltide_executor::sources::{
    DriftedSource, Pipeline, Plan, Preparation, Project, Recreation, Relation, Resource,
    ResourceRef, drifted_sources, plan_preserved,
};

/// `(kind, name, strings, pairs)`: a managed source has strings `[broker list, topic, format,
/// consumer group]` and its settings as pairs, a table has its `(name, type)` columns as pairs,
/// and a materialized view has strings `[source relation, target relation]`.
type ResourceArgument = (String, String, Vec<String>, Vec<(String, String)>);
/// `(name, repr of name, direct, source key, model keys, table transform sources)`.
type PipelineArgument = (String, String, bool, Option<usize>, Vec<usize>, Vec<String>);
/// `(key names, pipelines, source keys, resources per key)`.
type ProjectArgument = (
    Vec<String>,
    Vec<PipelineArgument>,
    Vec<usize>,
    Vec<Vec<ResourceArgument>>,
);
/// `(name, settings, (name, type) columns, target relation name)`.
type RelationArgument = (
    String,
    Vec<(String, String)>,
    Vec<(String, String)>,
    Option<String>,
);
/// `(execution keys, prerequisite keys, (selected pipeline, drifted source key))`.
type PlanArgument = (Vec<usize>, Vec<usize>, Vec<(String, usize)>);
/// `(managed source, landing view, consumer group, drifted setting names)`.
type RecreationResult = (ResourceRef, ResourceRef, String, Vec<String>);
/// `(preserved names, created names, landing view names, managed sources, recreations,
/// realizations)`.
type PreparationResult = (
    Vec<String>,
    Vec<String>,
    Vec<String>,
    Vec<ResourceRef>,
    Vec<RecreationResult>,
    Vec<ResourceRef>,
);

/// `(source key, sorted owner pipelines, drifted setting names)`.
type DriftedResult = (usize, Vec<String>, Vec<String>);

fn resource(argument: ResourceArgument) -> PyResult<Resource> {
    let (kind, name, strings, pairs) = argument;
    Ok(match (kind.as_str(), strings.as_slice()) {
        ("managed_source", [broker_list, topic, format, consumer_group]) => {
            Resource::ManagedSource {
                name,
                broker_list: broker_list.clone(),
                topic: topic.clone(),
                format: format.clone(),
                consumer_group: consumer_group.clone(),
                settings: pairs,
            }
        }
        ("table", _) => Resource::Table {
            name,
            columns: pairs,
        },
        ("materialized_view", [source_relation_name, target_relation_name]) => {
            Resource::MaterializedView {
                name,
                source_relation_name: source_relation_name.clone(),
                target_relation_name: target_relation_name.clone(),
            }
        }
        ("other", _) => Resource::Other,
        _ => {
            return Err(PyValueError::new_err(format!(
                "Invalid managed source resource '{kind}'"
            )));
        }
    })
}

fn project(argument: ProjectArgument) -> PyResult<Project> {
    let (key_names, pipelines, sources, resources) = argument;
    Ok(Project {
        key_names,
        pipelines: pipelines
            .into_iter()
            .map(
                |(name, quoted_name, direct, source, models, table_transform_sources)| Pipeline {
                    name,
                    quoted_name,
                    direct,
                    source,
                    models,
                    table_transform_sources,
                },
            )
            .collect(),
        sources,
        resources: resources
            .into_iter()
            .map(|resources| resources.into_iter().map(resource).collect())
            .collect::<PyResult<_>>()?,
    })
}

fn relations(arguments: Vec<RelationArgument>) -> Vec<Relation> {
    arguments
        .into_iter()
        .map(|(name, settings, columns, target_relation_name)| Relation {
            name,
            settings,
            columns,
            target_relation_name,
        })
        .collect()
}

/// How a direct build prepares managed source infrastructure in `database`.
#[pyfunction]
fn plan_preserved_managed_sources(
    plan: PlanArgument,
    project_argument: ProjectArgument,
    relations_argument: Vec<RelationArgument>,
    database: &str,
) -> PyResult<PreparationResult> {
    let (execution, prerequisites, drift_selections) = plan;
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
        &project(project_argument)?,
        &relations(relations_argument),
        database,
    )
    .map_err(PyValueError::new_err)?;
    Ok((
        preserved_relation_names,
        created_relation_names,
        landing_view_names,
        managed_sources,
        recreations
            .into_iter()
            .map(
                |Recreation {
                     managed_source,
                     landing_view,
                     consumer_group,
                     drifted_setting_names,
                 }| {
                    (
                        managed_source,
                        landing_view,
                        consumer_group,
                        drifted_setting_names,
                    )
                },
            )
            .collect(),
        realizations,
    ))
}

/// `(source key, sorted owner pipelines, drifted setting names)` for each deployed managed
/// source whose settings drifted, ordered by source name.
#[pyfunction]
fn drifted_managed_sources(
    project_argument: ProjectArgument,
    relations_argument: Vec<RelationArgument>,
) -> PyResult<Vec<DriftedResult>> {
    Ok(
        drifted_sources(&project(project_argument)?, &relations(relations_argument))
            .into_iter()
            .map(
                |DriftedSource {
                     source,
                     owner_pipeline_names,
                     drifted_setting_names,
                 }| (source, owner_pipeline_names, drifted_setting_names),
            )
            .collect(),
    )
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_preserved_managed_sources, module)?)?;
    module.add_function(wrap_pyfunction!(drifted_managed_sources, module)?)
}
