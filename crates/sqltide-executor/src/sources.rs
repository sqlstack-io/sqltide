//! Managed source infrastructure a direct build preserves, recreates, or refuses to touch.
//!
//! A direct build keeps each relevant source's Kafka table, landing table, and landing view.
//! A Kafka table whose declared settings drifted is recreated when a selected pipeline owns it.
//! Any other drift blocks the build.

use std::collections::{HashMap, HashSet};

use sqltide_clickhouse::resources::database_scoped_consumer_group;
use sqltide_discovery::text::is_python_space;

const BROKER_LIST_SETTING: &str = "kafka_broker_list";
const TOPIC_LIST_SETTING: &str = "kafka_topic_list";
const FORMAT_SETTING: &str = "kafka_format";
const GROUP_NAME_SETTING: &str = "kafka_group_name";
const MASKED_SETTING_VALUE: &str = "[HIDDEN]";
const DRIFT_PREFIX: &str =
    "Direct build preserves managed source infrastructure and cannot continue while it has drifted";

/// One adapter resource realized for a logical key.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Resource {
    ManagedSource {
        name: String,
        broker_list: String,
        topic: String,
        format: String,
        consumer_group: String,
        settings: Vec<(String, String)>,
    },
    Table {
        name: String,
        columns: Vec<(String, String)>,
    },
    MaterializedView {
        name: String,
        source_relation_name: String,
        target_relation_name: String,
    },
    /// A resource source preparation ignores.
    Other,
}

impl Resource {
    fn name(&self) -> Option<&str> {
        match self {
            Self::ManagedSource { name, .. }
            | Self::Table { name, .. }
            | Self::MaterializedView { name, .. } => Some(name),
            Self::Other => None,
        }
    }
}

/// One compiled pipeline.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Pipeline {
    pub name: String,
    /// The name as Python's `repr` renders it, for messages.
    pub quoted_name: String,
    pub direct: bool,
    pub source: Option<usize>,
    pub models: Vec<usize>,
    /// The source each table model's transform reads.
    pub table_transform_sources: Vec<String>,
}

/// The realized project, with logical keys as positions into `key_names`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Project {
    pub key_names: Vec<String>,
    pub pipelines: Vec<Pipeline>,
    /// Source keys in declaration order.
    pub sources: Vec<usize>,
    /// Resources per key; keys with none are empty.
    pub resources: Vec<Vec<Resource>>,
}

/// One relation observed in the warehouse.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Relation {
    pub name: String,
    pub settings: Vec<(String, String)>,
    pub columns: Vec<(String, String)>,
    pub target_relation_name: Option<String>,
}

/// The direct plan's scope.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Plan {
    pub execution: Vec<usize>,
    pub prerequisites: Vec<usize>,
    /// `(selected pipeline, drifted source key)`.
    pub drift_selections: Vec<(String, usize)>,
}

/// A resource, as `(key, position in that key's resources)`.
pub type ResourceRef = (usize, usize);

/// A Kafka table to drop and recreate with its declared settings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Recreation {
    pub managed_source: ResourceRef,
    pub landing_view: ResourceRef,
    pub consumer_group: String,
    pub drifted_setting_names: Vec<String>,
}

/// How a direct build prepares managed source infrastructure.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Preparation {
    pub preserved_relation_names: Vec<String>,
    pub created_relation_names: Vec<String>,
    pub landing_view_names: Vec<String>,
    pub managed_sources: Vec<ResourceRef>,
    pub recreations: Vec<Recreation>,
    /// Absent Kafka and landing tables to create.
    pub realizations: Vec<ResourceRef>,
}

/// A deployed managed source whose settings drifted.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DriftedSource {
    pub source: usize,
    pub owner_pipeline_names: Vec<String>,
    pub drifted_setting_names: Vec<String>,
}

struct Catalog<'a> {
    relations: HashMap<&'a str, &'a Relation>,
}

impl<'a> Catalog<'a> {
    fn new(relations: &'a [Relation]) -> Self {
        let mut by_name: HashMap<&str, &Relation> = HashMap::new();
        for relation in relations {
            by_name.entry(&relation.name).or_insert(relation);
        }
        Self { relations: by_name }
    }

    fn relation(&self, name: &str) -> Option<&'a Relation> {
        self.relations.get(name).copied()
    }
}

/// Validate preserved sources and plan absent or recreated resources.
///
/// # Errors
///
/// Returns the refusal message when preserved infrastructure drifted in a way the build
/// cannot repair, or a recreated Kafka table has no landing view.
pub fn plan_preserved(
    plan: &Plan,
    project: &Project,
    relations: &[Relation],
    database: &str,
) -> Result<Preparation, String> {
    let catalog: Catalog<'_> = Catalog::new(relations);
    let readers: Vec<(usize, Vec<String>)> = recreating_pipelines(plan, project);
    let source_keys: HashSet<usize> = readers.iter().map(|(key, _)| *key).collect();
    let resources: Vec<(ResourceRef, &Resource)> = source_resources(project, &source_keys);
    let recreations: Vec<Recreation> = plan_recreations(
        &resources,
        &catalog,
        database,
        &unselected_hints(project, &readers),
        &selection_notes(plan, project),
    )?;
    let recreated: HashSet<ResourceRef> = recreations
        .iter()
        .map(|recreation| recreation.managed_source)
        .collect();
    let recreated_names: HashSet<&str> = resources
        .iter()
        .filter(|(reference, _)| recreated.contains(reference))
        .filter_map(|(_, resource)| resource.name())
        .collect();
    let mut preparation: Preparation = Preparation::default();
    for &(reference, resource) in &resources {
        let name: &str = resource.name().unwrap_or_default();
        if recreated_names.contains(name) {
            continue;
        }
        if catalog.relation(name).is_some() {
            preparation.preserved_relation_names.push(name.to_owned());
            continue;
        }
        preparation.created_relation_names.push(name.to_owned());
        if matches!(resource, Resource::MaterializedView { .. }) {
            preparation.landing_view_names.push(name.to_owned());
        } else {
            preparation.realizations.push(reference);
        }
    }
    preparation.managed_sources = resources
        .iter()
        .filter(|(_, resource)| matches!(resource, Resource::ManagedSource { .. }))
        .map(|(reference, _)| *reference)
        .collect();
    preparation.recreations = recreations;
    Ok(preparation)
}

/// Each deployed managed source whose settings drifted, ordered by source name, with its sorted
/// owning pipelines.
#[must_use]
pub fn drifted_sources(project: &Project, relations: &[Relation]) -> Vec<DriftedSource> {
    let catalog: Catalog<'_> = Catalog::new(relations);
    let mut owners: Vec<(usize, Vec<String>)> = source_owners(project);
    owners
        .sort_by(|(left, _), (right, _)| project.key_names[*left].cmp(&project.key_names[*right]));
    let mut drifted: Vec<DriftedSource> = Vec::new();
    for (key, mut owner_names) in owners {
        let mut drifted_setting_names: Vec<String> = Vec::new();
        for (_, resource) in source_resources(project, &HashSet::from([key])) {
            if let Some(relation) = resource.name().and_then(|name| catalog.relation(name)) {
                drifted_setting_names.extend(settings_drift(resource, relation));
            }
        }
        if !drifted_setting_names.is_empty() {
            owner_names.sort();
            drifted.push(DriftedSource {
                source: key,
                owner_pipeline_names: owner_names,
                drifted_setting_names,
            });
        }
    }
    drifted
}

/// Each source's direct readers, or every declaring pipeline when none reads it, in first
/// declaration order.
fn source_owners(project: &Project) -> Vec<(usize, Vec<String>)> {
    let mut order: Vec<usize> = Vec::new();
    let mut declaring: HashMap<usize, Vec<String>> = HashMap::new();
    let mut reading: HashMap<usize, Vec<String>> = HashMap::new();
    for pipeline in &project.pipelines {
        let Some(source) = pipeline.source else {
            continue;
        };
        declaring
            .entry(source)
            .or_insert_with(|| {
                order.push(source);
                Vec::new()
            })
            .push(pipeline.name.clone());
        let source_name: &str = &project.key_names[source];
        if pipeline
            .table_transform_sources
            .iter()
            .any(|name| name == source_name)
        {
            reading
                .entry(source)
                .or_default()
                .push(pipeline.name.clone());
        }
    }
    order
        .into_iter()
        .map(|source| {
            let owners: Vec<String> = reading
                .remove(&source)
                .unwrap_or_else(|| declaring.remove(&source).unwrap_or_default());
            (source, owners)
        })
        .collect()
}

/// Each source the plan reads, with the owning pipelines a build must select to recreate it:
/// none when the build already executes an owner.
fn recreating_pipelines(plan: &Plan, project: &Project) -> Vec<(usize, Vec<String>)> {
    let execution: HashSet<usize> = plan.execution.iter().copied().collect();
    let relevant: HashSet<usize> = plan
        .execution
        .iter()
        .chain(&plan.prerequisites)
        .copied()
        .collect();
    let mut source_keys: Vec<usize> = Vec::new();
    let mut executed: HashSet<&str> = HashSet::new();
    for pipeline in &project.pipelines {
        let Some(source) = pipeline.source else {
            continue;
        };
        if pipeline.models.iter().any(|model| relevant.contains(model))
            && !source_keys.contains(&source)
        {
            source_keys.push(source);
        }
        if pipeline
            .models
            .iter()
            .any(|model| execution.contains(model))
        {
            executed.insert(&pipeline.name);
        }
    }
    let owners: HashMap<usize, Vec<String>> = source_owners(project).into_iter().collect();
    source_keys
        .into_iter()
        .map(|key| {
            let key_owners: &Vec<String> = &owners[&key];
            let readers: Vec<String> = if key_owners
                .iter()
                .any(|name| executed.contains(name.as_str()))
            {
                Vec::new()
            } else {
                key_owners.clone()
            };
            (key, readers)
        })
        .collect()
}

fn source_resources<'a>(
    project: &'a Project,
    keys: &HashSet<usize>,
) -> Vec<(ResourceRef, &'a Resource)> {
    project
        .sources
        .iter()
        .filter(|key| keys.contains(key))
        .flat_map(|&key| {
            project.resources[key]
                .iter()
                .enumerate()
                .filter(|(_, resource)| !matches!(resource, Resource::Other))
                .map(move |(position, resource)| ((key, position), resource))
        })
        .collect()
}

/// Per Kafka table name, how to select a build that may recreate the source.
fn unselected_hints(
    project: &Project,
    readers: &[(usize, Vec<String>)],
) -> HashMap<String, String> {
    let quoted: HashMap<&str, &str> = project
        .pipelines
        .iter()
        .map(|pipeline| (pipeline.name.as_str(), pipeline.quoted_name.as_str()))
        .collect();
    let direct: HashSet<&str> = project
        .pipelines
        .iter()
        .filter(|pipeline| pipeline.direct)
        .map(|pipeline| pipeline.name.as_str())
        .collect();
    let mut hints: HashMap<String, String> = HashMap::new();
    for (key, names) in readers {
        if names.is_empty() {
            continue;
        }
        let mut sorted: Vec<&str> = names.iter().map(String::as_str).collect();
        sorted.sort_unstable();
        let reader_list: String = sorted
            .iter()
            .map(|name| quoted.get(name).copied().unwrap_or(name))
            .collect::<Vec<&str>>()
            .join(", ");
        let direct_readers: Vec<&str> = sorted
            .iter()
            .copied()
            .filter(|name| direct.contains(name))
            .collect();
        let selected: &[&str] = if direct_readers.is_empty() {
            &sorted
        } else {
            &direct_readers[..1]
        };
        let selectors: String = selected
            .iter()
            .map(|name| format!("pipeline:{name}"))
            .collect::<Vec<String>>()
            .join(", ");
        let hint: String = format!(
            "source '{}' is read directly only by pipeline {reader_list}, which this build does \
             not rebuild; include {selectors} in the selection to recreate it with the declared \
             settings",
            project.key_names[*key]
        );
        for resource in &project.resources[*key] {
            if let Resource::ManagedSource { name, .. } = resource {
                hints.insert(name.clone(), hint.clone());
            }
        }
    }
    hints
}

/// Per source relation name, why a changed selection rebuilds the owning pipeline.
fn selection_notes(plan: &Plan, project: &Project) -> HashMap<String, String> {
    let mut notes: HashMap<String, String> = HashMap::new();
    for (pipeline_name, source) in &plan.drift_selections {
        let note: String = format!(
            "pipeline '{pipeline_name}' was selected because source '{}' settings drifted from \
             the project, and ",
            project.key_names[*source]
        );
        for name in project.resources[*source].iter().filter_map(Resource::name) {
            notes.insert(name.to_owned(), note.clone());
        }
    }
    notes
}

fn plan_recreations(
    resources: &[(ResourceRef, &Resource)],
    catalog: &Catalog<'_>,
    database: &str,
    hints: &HashMap<String, String>,
    notes: &HashMap<String, String>,
) -> Result<Vec<Recreation>, String> {
    let mut blocking: Vec<String> = Vec::new();
    let mut prerequisite: Vec<String> = Vec::new();
    let mut recreations: Vec<Recreation> = Vec::new();
    for &(reference, resource) in resources {
        let Some(name) = resource.name() else {
            continue;
        };
        let Some(relation) = catalog.relation(name) else {
            continue;
        };
        let note: &str = notes.get(name).map_or("", String::as_str);
        match resource {
            Resource::ManagedSource { consumer_group, .. } => {
                let drifted: Vec<String> = settings_drift(resource, relation);
                if drifted.is_empty() {
                    continue;
                }
                if let Some(hint) = hints.get(name) {
                    prerequisite.push(format!(
                        "{} settings ({}) differ from the project declaration, but {hint}",
                        relation.name,
                        drifted.join(", ")
                    ));
                    continue;
                }
                let consumer_group: String =
                    database_scoped_consumer_group(consumer_group, database);
                let actual_group: Option<String> =
                    actual_settings(relation).remove(GROUP_NAME_SETTING);
                if actual_group.as_deref() != Some(consumer_group.as_str()) {
                    blocking.push(format!(
                        "{note}{} consumes as consumer group '{}' but the project declares \
                         '{consumer_group}', so recreating it for its changed settings would not \
                         resume from the committed offsets",
                        relation.name,
                        actual_group.as_deref().unwrap_or("None")
                    ));
                }
                let landing_view: ResourceRef = resources
                    .iter()
                    .find(|(_, candidate)| {
                        matches!(candidate, Resource::MaterializedView { source_relation_name, .. } if source_relation_name == name)
                    })
                    .map(|(candidate, _)| *candidate)
                    .ok_or_else(|| format!("Managed source '{name}' has no landing view"))?;
                recreations.push(Recreation {
                    managed_source: reference,
                    landing_view,
                    consumer_group,
                    drifted_setting_names: drifted,
                });
            }
            Resource::Table { columns, .. } => {
                if *columns != relation.columns {
                    blocking.push(format!(
                        "{note}{} landing columns no longer match the project declaration",
                        relation.name
                    ));
                }
            }
            Resource::MaterializedView {
                target_relation_name,
                ..
            } => {
                if relation.target_relation_name.as_ref() != Some(target_relation_name) {
                    blocking.push(format!(
                        "{note}{} writes to '{}' but the project declares '{target_relation_name}'",
                        relation.name,
                        relation.target_relation_name.as_deref().unwrap_or("None")
                    ));
                }
            }
            Resource::Other => {}
        }
    }
    if !blocking.is_empty() {
        blocking.extend(prerequisite);
        return Err(format!(
            "{DRIFT_PREFIX}: {}. Recreate the source explicitly before rebuilding.",
            blocking.join("; ")
        ));
    }
    if !prerequisite.is_empty() {
        return Err(format!("{DRIFT_PREFIX}: {}.", prerequisite.join("; ")));
    }
    Ok(recreations)
}

/// The declared Kafka settings, in declaration order, whose live value differs and is not
/// masked. Only Kafka tables have settings to compare.
fn settings_drift(resource: &Resource, relation: &Relation) -> Vec<String> {
    let Resource::ManagedSource {
        broker_list,
        topic,
        format,
        settings,
        ..
    } = resource
    else {
        return Vec::new();
    };
    let mut expected: Vec<(&str, &str)> = vec![
        (BROKER_LIST_SETTING, broker_list),
        (TOPIC_LIST_SETTING, topic),
        (FORMAT_SETTING, format),
    ];
    for (name, value) in settings {
        match expected.iter_mut().find(|(existing, _)| existing == name) {
            Some(entry) => entry.1 = value,
            None => expected.push((name, value)),
        }
    }
    let actual: HashMap<String, String> = actual_settings(relation);
    expected
        .into_iter()
        .filter(|(name, value)| {
            actual
                .get(*name)
                .is_none_or(|actual| actual != value && actual != MASKED_SETTING_VALUE)
        })
        .map(|(name, _)| name.to_owned())
        .collect()
}

fn actual_settings(relation: &Relation) -> HashMap<String, String> {
    relation
        .settings
        .iter()
        .map(|(name, value)| (name.clone(), unquoted(value).to_owned()))
        .collect()
}

/// `value.strip().strip("'")`, with Python's whitespace.
fn unquoted(value: &str) -> &str {
    value.trim_matches(is_python_space).trim_matches('\'')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kafka(name: &str, topic: &str) -> Resource {
        Resource::ManagedSource {
            name: name.to_owned(),
            broker_list: "broker:9092".to_owned(),
            topic: topic.to_owned(),
            format: "JSONEachRow".to_owned(),
            consumer_group: "orders".to_owned(),
            settings: vec![("kafka_num_consumers".to_owned(), "2".to_owned())],
        }
    }

    fn landing_table(name: &str) -> Resource {
        Resource::Table {
            name: name.to_owned(),
            columns: vec![("id".to_owned(), "UInt64".to_owned())],
        }
    }

    fn landing_view(name: &str, source: &str, target: &str) -> Resource {
        Resource::MaterializedView {
            name: name.to_owned(),
            source_relation_name: source.to_owned(),
            target_relation_name: target.to_owned(),
        }
    }

    fn kafka_relation(topic: &str, group: &str) -> Relation {
        Relation {
            name: "orders_kafka".to_owned(),
            settings: vec![
                ("kafka_broker_list".to_owned(), " 'broker:9092' ".to_owned()),
                ("kafka_topic_list".to_owned(), format!("'{topic}'")),
                ("kafka_format".to_owned(), "[HIDDEN]".to_owned()),
                ("kafka_group_name".to_owned(), format!("'{group}'")),
                ("kafka_num_consumers".to_owned(), "2".to_owned()),
            ],
            columns: Vec::new(),
            target_relation_name: None,
        }
    }

    fn landing_relation() -> Relation {
        Relation {
            name: "orders_landing".to_owned(),
            settings: Vec::new(),
            columns: vec![("id".to_owned(), "UInt64".to_owned())],
            target_relation_name: None,
        }
    }

    fn pipeline(name: &str, direct: bool, models: Vec<usize>, reads: bool) -> Pipeline {
        Pipeline {
            name: name.to_owned(),
            quoted_name: format!("'{name}'"),
            direct,
            source: Some(0),
            models,
            table_transform_sources: if reads {
                vec!["orders".to_owned()]
            } else {
                Vec::new()
            },
        }
    }

    /// Key 0 is source `orders`; keys 1 and 2 are models of pipelines `ingest` and `report`.
    fn project(pipelines: Vec<Pipeline>) -> Project {
        Project {
            key_names: vec![
                "orders".to_owned(),
                "orders_clean".to_owned(),
                "orders_report".to_owned(),
            ],
            pipelines,
            sources: vec![0],
            resources: vec![
                vec![
                    kafka("orders_kafka", "orders"),
                    Resource::Other,
                    landing_table("orders_landing"),
                    landing_view("orders_mv", "orders_kafka", "orders_landing"),
                ],
                Vec::new(),
                Vec::new(),
            ],
        }
    }

    fn plan(execution: Vec<usize>) -> Plan {
        Plan {
            execution,
            ..Plan::default()
        }
    }

    #[test]
    fn preserves_matching_resources_and_creates_absent_ones() {
        let project = project(vec![pipeline("ingest", true, vec![1], true)]);
        let relations = vec![kafka_relation("orders", "orders_main"), landing_relation()];
        let preparation = plan_preserved(&plan(vec![1]), &project, &relations, "main").unwrap();
        assert_eq!(
            preparation.preserved_relation_names,
            vec!["orders_kafka", "orders_landing"]
        );
        assert_eq!(preparation.created_relation_names, vec!["orders_mv"]);
        assert_eq!(preparation.landing_view_names, vec!["orders_mv"]);
        assert_eq!(preparation.managed_sources, vec![(0, 0)]);
        assert!(preparation.realizations.is_empty());
        assert!(preparation.recreations.is_empty());
    }

    #[test]
    fn recreates_drifted_kafka_tables_an_executed_owner_rebuilds() {
        let project = project(vec![pipeline("ingest", true, vec![1], true)]);
        let relations = vec![
            kafka_relation("old_orders", "orders_main"),
            landing_relation(),
        ];
        let preparation = plan_preserved(&plan(vec![1]), &project, &relations, "main").unwrap();
        assert_eq!(
            preparation.recreations,
            vec![Recreation {
                managed_source: (0, 0),
                landing_view: (0, 3),
                consumer_group: "orders_main".to_owned(),
                drifted_setting_names: vec!["kafka_topic_list".to_owned()],
            }]
        );
        assert_eq!(preparation.preserved_relation_names, vec!["orders_landing"]);
        assert_eq!(preparation.realizations, Vec::<ResourceRef>::new());
    }

    #[test]
    fn blocks_drift_that_would_lose_offsets_or_needs_another_pipeline() {
        let project = project(vec![
            pipeline("ingest", false, vec![1], true),
            pipeline("report", true, vec![2], false),
        ]);
        let relations = vec![
            kafka_relation("old_orders", "other_group"),
            landing_relation(),
        ];
        let mut selected = plan(vec![1]);
        selected.drift_selections = vec![("ingest".to_owned(), 0)];
        assert_eq!(
            plan_preserved(&selected, &project, &relations, "main").unwrap_err(),
            "Direct build preserves managed source infrastructure and cannot continue while it \
             has drifted: pipeline 'ingest' was selected because source 'orders' settings drifted \
             from the project, and orders_kafka consumes as consumer group 'other_group' but the \
             project declares 'orders_main', so recreating it for its changed settings would not \
             resume from the committed offsets. Recreate the source explicitly before rebuilding."
        );
        assert_eq!(
            plan_preserved(&plan(vec![2]), &project, &relations, "main").unwrap_err(),
            "Direct build preserves managed source infrastructure and cannot continue while it \
             has drifted: orders_kafka settings (kafka_topic_list) differ from the project \
             declaration, but source 'orders' is read directly only by pipeline 'ingest', which \
             this build does not rebuild; include pipeline:ingest in the selection to recreate it \
             with the declared settings."
        );
    }

    #[test]
    fn reports_drifted_sources_with_sorted_owners() {
        let project = project(vec![
            pipeline("zeta", false, vec![1], false),
            pipeline("alpha", false, vec![2], false),
        ]);
        assert_eq!(
            drifted_sources(&project, &[kafka_relation("old_orders", "g")]),
            vec![DriftedSource {
                source: 0,
                owner_pipeline_names: vec!["alpha".to_owned(), "zeta".to_owned()],
                drifted_setting_names: vec!["kafka_topic_list".to_owned()],
            }]
        );
        assert!(drifted_sources(&project, &[kafka_relation("orders", "g")]).is_empty());
    }
}
