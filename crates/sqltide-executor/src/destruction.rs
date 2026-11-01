//! Destruction planning: which pipelines a destructive request reaches, which warehouse
//! relations those pipelines own, and the frozen evidence and fingerprint of the plan.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use sqltide_json::{JsonDialect, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions, dumps};

const METADATA_RELATION_PREFIX: &str = "_sqltide_";
const MAX_NAMED_CHALLENGES: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Operation {
    DestroyPipelines,
    ResetTarget,
}

impl Operation {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DestroyPipelines => "destroy_pipelines",
            Self::ResetTarget => "reset_target",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RelationKind {
    View,
    MaterializedView,
    Table,
    ManagedSource,
}

impl RelationKind {
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "view" => Self::View,
            "materialized_view" => Self::MaterializedView,
            "table" => Self::Table,
            "managed_source" => Self::ManagedSource,
            _ => return None,
        })
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::View => "view",
            Self::MaterializedView => "materialized_view",
            Self::Table => "table",
            Self::ManagedSource => "managed_source",
        }
    }

    /// Dependants drop before what they read, so lower ranks win when evidence disagrees.
    const fn drop_rank(self) -> u8 {
        match self {
            Self::View => 0,
            Self::MaterializedView => 1,
            Self::Table | Self::ManagedSource => 2,
        }
    }
}

/// How a relation is attributed to the plan, ordered by its serialized value.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Ownership {
    CurrentManifest,
    HistoricalManifest,
    PublishedStableBinding,
    VirtualPhysicalMapping,
}

impl Ownership {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::CurrentManifest => "current_manifest",
            Self::HistoricalManifest => "historical_manifest",
            Self::PublishedStableBinding => "published_stable_binding",
            Self::VirtualPhysicalMapping => "virtual_physical_mapping",
        }
    }
}

/// One logical key of the compiled project.
pub struct Key {
    pub name: String,
    pub source: bool,
    /// The owning pipeline of a model.
    pub pipeline: Option<String>,
    pub resources: Vec<(String, RelationKind)>,
}

/// The compiled project as interned keys and their dependency edges.
pub struct Project {
    pub keys: Vec<Key>,
    pub upstream: Vec<Vec<usize>>,
    pub downstream: Vec<Vec<usize>>,
    /// `(pipeline name, declared source key)` in project order.
    pub pipelines: Vec<(String, Option<usize>)>,
    pub sources: Vec<usize>,
    pub models: Vec<usize>,
}

pub struct Request {
    pub operation: Operation,
    pub database: String,
    pub pipeline_names: Vec<String>,
    pub included_dependent_pipeline_names: Vec<String>,
    pub include_orphans: bool,
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ManifestResource {
    pub resource_name: String,
    pub pipeline_name: String,
    pub logical_name: String,
    pub logical_type: String,
    pub resource_role: String,
    pub resource_database: String,
    pub resource_kind: String,
}

pub struct Manifest {
    pub published_at: String,
    pub resources: Vec<ManifestResource>,
}

/// Manifest history, newest first. `absent` means the metadata tables do not exist.
pub struct Snapshot {
    pub absent: bool,
    pub manifests: Vec<Manifest>,
}

pub struct Mapping {
    pub database: Option<String>,
    pub object_type: String,
    pub name: String,
    pub physical_name: String,
    pub logical_model_name: String,
}

pub struct Deployment {
    pub deployment_id: String,
    pub mappings: Vec<Mapping>,
}

pub struct Binding {
    pub database: String,
    pub logical_name: String,
    pub physical_name: String,
}

pub struct PublishEvent {
    pub deployment_id: String,
    pub published_at: String,
    pub bindings: Vec<Binding>,
}

pub struct Inventory {
    pub deployments: Vec<Deployment>,
    pub publish_events: Vec<PublishEvent>,
}

pub struct CatalogRelation {
    pub name: String,
    pub engine: String,
    pub ownership_generation: Option<String>,
    /// Source, target, and stable-binding relation names.
    pub dependency_names: Vec<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum DestructionError {
    Selection(String),
    UnknownPipelines(Vec<String>),
    Dependency(Vec<String>),
    Resource(String),
    UnsupportedHistoricalKind { kind: String, name: String },
    InvalidObjectType(String),
}

/// A relation attributed to the plan, before warehouse evidence is attached.
#[derive(Debug, PartialEq, Eq)]
pub struct OwnedRelation {
    pub name: String,
    pub kind: RelationKind,
    pub logical_names: BTreeSet<String>,
    pub pipeline_names: BTreeSet<String>,
    pub ownership: BTreeSet<Ownership>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Scope {
    pub affected_model_names: Vec<String>,
    pub affected_source_names: Vec<String>,
    /// Sorted by relation name.
    pub owned: Vec<OwnedRelation>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Evidence {
    pub database: String,
    pub name: String,
    pub kind: RelationKind,
    pub exists: bool,
    pub total_bytes: Option<i128>,
    pub active_parts: Option<i128>,
    pub catalog_fingerprint: Option<String>,
    pub logical_names: Vec<String>,
    pub pipeline_names: Vec<String>,
    pub ownership: Vec<Ownership>,
    pub dependency_relation_names: Vec<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct InactivePipeline {
    pub name: String,
    pub model_count: usize,
    pub resource_count: usize,
    pub last_published_at: String,
}

/// Whether `name` is framework metadata, matching `name.casefold().startswith("_sqltide_")`.
///
/// Only ASCII letters and `ſ` casefold to a single character of the prefix; every multi-character
/// casefold that starts with a prefix letter continues with a character the prefix lacks.
#[must_use]
pub fn is_metadata_relation(name: &str) -> bool {
    let mut folded = name.chars().map(|character| match character {
        'ſ' => 's',
        _ => character.to_ascii_lowercase(),
    });
    METADATA_RELATION_PREFIX
        .chars()
        .all(|expected| folded.next() == Some(expected))
}

fn sorted_unique<'a>(names: impl IntoIterator<Item = &'a String>) -> Vec<String> {
    names
        .into_iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

impl Project {
    /// `roots` and every key reachable from them in one direction, in no particular order.
    fn reachable(
        &self,
        roots: impl IntoIterator<Item = usize>,
        downstream: bool,
    ) -> HashSet<usize> {
        let edges = if downstream {
            &self.downstream
        } else {
            &self.upstream
        };
        let mut visited: HashSet<usize> = roots.into_iter().collect();
        let mut stack: Vec<usize> = visited.iter().copied().collect();
        while let Some(key) = stack.pop() {
            for &next in &edges[key] {
                if visited.insert(next) {
                    stack.push(next);
                }
            }
        }
        visited
    }

    fn models_in<'a>(&'a self, pipelines: &'a HashSet<&str>) -> impl Iterator<Item = usize> + 'a {
        self.models.iter().copied().filter(move |&model| {
            self.keys[model]
                .pipeline
                .as_deref()
                .is_some_and(|pipeline| pipelines.contains(pipeline))
        })
    }

    fn model_pipelines(&self, keys: &HashSet<usize>) -> impl Iterator<Item = &String> {
        self.models
            .iter()
            .filter(|model| keys.contains(model))
            .filter_map(|&model| self.keys[model].pipeline.as_ref())
    }

    fn managed_sources(&self) -> impl Iterator<Item = usize> + '_ {
        self.sources
            .iter()
            .copied()
            .filter(|&source| !self.keys[source].resources.is_empty())
    }

    /// Every pipeline that owns or reads each logical name. Every source gets an entry.
    fn pipeline_names_by_logical_name(&self) -> HashMap<String, Vec<String>> {
        let mut names: HashMap<String, BTreeSet<String>> = HashMap::new();
        for &model in &self.models {
            let key = &self.keys[model];
            if let Some(pipeline) = &key.pipeline {
                names
                    .entry(key.name.clone())
                    .or_default()
                    .insert(pipeline.clone());
            }
        }
        for (pipeline, source) in &self.pipelines {
            if let Some(source) = source {
                names
                    .entry(self.keys[*source].name.clone())
                    .or_default()
                    .insert(pipeline.clone());
            }
        }
        for &source in &self.sources {
            let downstream = self.reachable([source], true);
            let pipelines: Vec<String> = self.model_pipelines(&downstream).cloned().collect();
            names
                .entry(self.keys[source].name.clone())
                .or_default()
                .extend(pipelines);
        }
        names
            .into_iter()
            .map(|(name, pipelines)| (name, pipelines.into_iter().collect()))
            .collect()
    }

    fn shared_source_pipeline_names(
        &self,
        pipelines: &HashSet<&str>,
        by_name: &HashMap<String, Vec<String>>,
    ) -> BTreeSet<String> {
        let managed: HashSet<&str> = self
            .managed_sources()
            .map(|source| self.keys[source].name.as_str())
            .collect();
        let mut source_names: HashSet<&str> = self
            .pipelines
            .iter()
            .filter(|(name, _)| pipelines.contains(name.as_str()))
            .filter_map(|(_, source)| source.map(|source| self.keys[source].name.as_str()))
            .filter(|name| managed.contains(name))
            .collect();
        let upstream = self.reachable(self.models_in(pipelines), false);
        source_names.extend(
            upstream
                .iter()
                .map(|&key| &self.keys[key])
                .filter(|key| key.source && managed.contains(key.name.as_str()))
                .map(|key| key.name.as_str()),
        );
        source_names
            .into_iter()
            .filter_map(|name| by_name.get(name))
            .flatten()
            .cloned()
            .collect()
    }
}

/// The current pipelines plus every pipeline only manifest history still owns.
#[must_use]
pub fn available_pipeline_names(current: &[String], snapshot: Option<&Snapshot>) -> Vec<String> {
    let mut names: BTreeSet<String> = current.iter().cloned().collect();
    if let Some(snapshot) = snapshot {
        let current: HashSet<&str> = current.iter().map(String::as_str).collect();
        for resources in latest_resources_by_name(snapshot).values() {
            if resources
                .iter()
                .any(|resource| current.contains(resource.pipeline_name.as_str()))
            {
                continue;
            }
            names.extend(
                resources
                    .iter()
                    .map(|resource| resource.pipeline_name.clone()),
            );
        }
    }
    names.into_iter().collect()
}

/// Resources of each name in the newest manifest that mentions it.
fn latest_resources_by_name(snapshot: &Snapshot) -> BTreeMap<&str, BTreeSet<&ManifestResource>> {
    let mut latest: BTreeMap<&str, BTreeSet<&ManifestResource>> = BTreeMap::new();
    for manifest in &snapshot.manifests {
        let mut by_name: BTreeMap<&str, BTreeSet<&ManifestResource>> = BTreeMap::new();
        for resource in &manifest.resources {
            by_name
                .entry(resource.resource_name.as_str())
                .or_default()
                .insert(resource);
        }
        for (name, resources) in by_name {
            latest.entry(name).or_insert(resources);
        }
    }
    latest
}

/// `(requested, included dependents, affected)` pipeline names.
pub type Selection = (Vec<String>, Vec<String>, Vec<String>);

/// The [`Selection`] of a destructive request.
///
/// # Errors
/// Rejects selections that are empty, unknown, or leave out a dependent pipeline.
pub fn resolve_selection(
    project: &Project,
    request: &Request,
    available: &[String],
) -> Result<Selection, DestructionError> {
    if request.operation == Operation::ResetTarget {
        if !request.pipeline_names.is_empty()
            || !request.included_dependent_pipeline_names.is_empty()
        {
            return Err(DestructionError::Selection(
                "Target reset does not accept a pipeline selection".to_owned(),
            ));
        }
        return Ok((Vec::new(), Vec::new(), available.to_vec()));
    }
    if request.pipeline_names.is_empty() {
        return Err(DestructionError::Selection(
            "Pipeline destruction requires at least one pipeline".to_owned(),
        ));
    }
    let supplied: BTreeSet<&str> = request
        .pipeline_names
        .iter()
        .chain(&request.included_dependent_pipeline_names)
        .map(String::as_str)
        .collect();
    let available: HashSet<&str> = available.iter().map(String::as_str).collect();
    let unknown: Vec<String> = supplied
        .iter()
        .filter(|name| !available.contains(*name))
        .map(|name| (*name).to_owned())
        .collect();
    if !unknown.is_empty() {
        return Err(DestructionError::UnknownPipelines(unknown));
    }
    let mut required: BTreeSet<String> = supplied.iter().map(|name| (*name).to_owned()).collect();
    let by_name = project.pipeline_names_by_logical_name();
    loop {
        let names: HashSet<&str> = required.iter().map(String::as_str).collect();
        let reachable = project.reachable(project.models_in(&names), true);
        let mut expanded = required.clone();
        expanded.extend(project.model_pipelines(&reachable).cloned());
        expanded.extend(project.shared_source_pipeline_names(&names, &by_name));
        if expanded == required {
            break;
        }
        required = expanded;
    }
    let selected: HashSet<&str> = request
        .pipeline_names
        .iter()
        .chain(&request.included_dependent_pipeline_names)
        .map(String::as_str)
        .collect();
    let missing: Vec<String> = required
        .into_iter()
        .filter(|name| !selected.contains(name.as_str()))
        .collect();
    if !missing.is_empty() {
        return Err(DestructionError::Dependency(missing));
    }
    Ok((
        request.pipeline_names.clone(),
        request.included_dependent_pipeline_names.clone(),
        supplied.into_iter().map(str::to_owned).collect(),
    ))
}

struct Owner<'a> {
    owned: BTreeMap<String, OwnedRelation>,
    pipeline_names: &'a HashMap<String, Vec<String>>,
}

impl Owner<'_> {
    fn add(
        &mut self,
        name: &str,
        kind: RelationKind,
        logical_name: &str,
        pipeline_name: Option<&str>,
        ownership: Ownership,
    ) {
        let relation = self
            .owned
            .entry(name.to_owned())
            .or_insert_with(|| OwnedRelation {
                name: name.to_owned(),
                kind,
                logical_names: BTreeSet::new(),
                pipeline_names: BTreeSet::new(),
                ownership: BTreeSet::new(),
            });
        if kind.drop_rank() < relation.kind.drop_rank() {
            relation.kind = kind;
        }
        relation.logical_names.insert(logical_name.to_owned());
        if let Some(pipeline_name) = pipeline_name {
            relation.pipeline_names.insert(pipeline_name.to_owned());
        }
        relation.ownership.insert(ownership);
    }

    /// Attribute `name` to every pipeline of the logical name `pipelines_of`, or to none.
    fn associate(
        &mut self,
        name: &str,
        kind: RelationKind,
        logical_name: &str,
        pipelines_of: &str,
        ownership: Ownership,
    ) {
        let pipelines: Vec<String> = self
            .pipeline_names
            .get(pipelines_of)
            .cloned()
            .unwrap_or_default();
        if pipelines.is_empty() {
            self.add(name, kind, logical_name, None, ownership);
        }
        for pipeline in &pipelines {
            self.add(name, kind, logical_name, Some(pipeline), ownership);
        }
    }
}

fn object_type_kind(object_type: &str) -> Result<RelationKind, DestructionError> {
    Ok(match object_type {
        "view" => RelationKind::View,
        "materialized_view" => RelationKind::MaterializedView,
        "kafka_table" => RelationKind::ManagedSource,
        "table" => RelationKind::Table,
        other => return Err(DestructionError::InvalidObjectType(other.to_owned())),
    })
}

/// Every relation the affected pipelines own, from the compiled project, manifest history, and
/// the deployment inventory.
///
/// # Errors
/// Rejects history and inventory records the plan cannot destroy safely.
#[allow(clippy::too_many_arguments)]
pub fn plan_scope(
    project: &Project,
    request: &Request,
    affected_pipelines: &[String],
    current_pipelines: &[String],
    snapshot: Option<&Snapshot>,
    catalog: &[CatalogRelation],
    inventory: &Inventory,
) -> Result<Scope, DestructionError> {
    let affected: HashSet<&str> = affected_pipelines.iter().map(String::as_str).collect();
    let current: HashSet<&str> = current_pipelines.iter().map(String::as_str).collect();
    let reset = request.operation == Operation::ResetTarget;
    let mut models: Vec<usize> = project.models_in(&affected).collect();
    models.sort_by(|left, right| project.keys[*left].name.cmp(&project.keys[*right].name));
    let mut model_names: BTreeSet<String> = models
        .iter()
        .map(|&model| project.keys[model].name.clone())
        .collect();
    let sources = affected_sources(project, reset, &affected);
    let mut source_names: BTreeSet<String> = sources
        .iter()
        .map(|&source| project.keys[source].name.clone())
        .collect();
    let mut pipeline_names = project.pipeline_names_by_logical_name();
    if let Some(snapshot) = snapshot {
        for manifest in &snapshot.manifests {
            let mut by_logical_name: BTreeMap<&str, BTreeSet<String>> = BTreeMap::new();
            for resource in &manifest.resources {
                by_logical_name
                    .entry(&resource.logical_name)
                    .or_default()
                    .insert(resource.pipeline_name.clone());
            }
            for (logical_name, pipelines) in by_logical_name {
                pipeline_names
                    .entry(logical_name.to_owned())
                    .or_insert_with(|| pipelines.into_iter().collect());
            }
        }
    }
    let mut owner = Owner {
        owned: BTreeMap::new(),
        pipeline_names: &pipeline_names,
    };
    let mut selected: Vec<usize> = models.iter().chain(&sources).copied().collect();
    selected.sort_by_key(|&key| (project.keys[key].source, &project.keys[key].name));
    selected.dedup();
    for key in selected {
        let key = &project.keys[key];
        for (name, kind) in &key.resources {
            if !is_metadata_relation(name) {
                owner.associate(
                    name,
                    *kind,
                    &key.name,
                    &key.name,
                    Ownership::CurrentManifest,
                );
            }
        }
    }
    if reset {
        let recorded = inventory
            .deployments
            .iter()
            .flat_map(|deployment| &deployment.mappings)
            .map(|mapping| &mapping.logical_model_name)
            .filter(|name| !source_names.contains(*name))
            .cloned()
            .collect::<Vec<_>>();
        model_names.extend(recorded);
    }
    let inactive: HashSet<&str> = affected.difference(&current).copied().collect();
    if let Some(snapshot) = snapshot.filter(|_| request.include_orphans || !inactive.is_empty()) {
        add_historical_relations(
            &mut owner, project, request, snapshot, catalog, &affected, &inactive,
        )?;
        let (historical_models, historical_sources) =
            historical_logical_names(project, snapshot, &affected);
        model_names.extend(historical_models);
        source_names.extend(historical_sources);
    }
    let affected_logical: HashSet<&str> = model_names
        .iter()
        .chain(&source_names)
        .map(String::as_str)
        .collect();
    add_inventory_relations(
        &mut owner,
        inventory,
        &request.database,
        &affected_logical,
        reset,
    )?;
    Ok(Scope {
        affected_model_names: model_names.into_iter().collect(),
        affected_source_names: source_names.into_iter().collect(),
        owned: owner.owned.into_values().collect(),
    })
}

fn affected_sources(project: &Project, reset: bool, affected: &HashSet<&str>) -> Vec<usize> {
    let managed = project.managed_sources();
    if reset {
        return managed.collect();
    }
    let upstream = project.reachable(project.models_in(affected), false);
    let declared: HashSet<usize> = project
        .pipelines
        .iter()
        .filter(|(name, _)| affected.contains(name.as_str()))
        .filter_map(|(_, source)| *source)
        .collect();
    managed
        .filter(|source| upstream.contains(source) || declared.contains(source))
        .collect()
}

fn add_historical_relations(
    owner: &mut Owner<'_>,
    project: &Project,
    request: &Request,
    snapshot: &Snapshot,
    catalog: &[CatalogRelation],
    affected: &HashSet<&str>,
    allow_published: &HashSet<&str>,
) -> Result<(), DestructionError> {
    if snapshot.absent {
        return Ok(());
    }
    let Some(newest) = snapshot.manifests.first() else {
        return Ok(());
    };
    let current: HashSet<&str> = project
        .keys
        .iter()
        .flat_map(|key| &key.resources)
        .map(|(name, _)| name.as_str())
        .filter(|name| !is_metadata_relation(name))
        .collect();
    let mut published: HashMap<&str, HashSet<&str>> = HashMap::new();
    for resource in &newest.resources {
        published
            .entry(&resource.resource_name)
            .or_default()
            .insert(&resource.pipeline_name);
    }
    let mut eligible: BTreeSet<&ManifestResource> = BTreeSet::new();
    for (name, resources) in latest_resources_by_name(snapshot) {
        let protected = published
            .get(name)
            .is_some_and(|pipelines| !pipelines.iter().all(|name| allow_published.contains(name)));
        if current.contains(name)
            || protected
            || is_metadata_relation(name)
            || !catalog.iter().any(|relation| relation.name == name)
        {
            continue;
        }
        eligible.extend(resources.into_iter().filter(|resource| {
            affected.contains(resource.pipeline_name.as_str())
                && resource.resource_database == request.database
        }));
    }
    for resource in eligible {
        let kind = RelationKind::parse(&resource.resource_kind).ok_or_else(|| {
            DestructionError::UnsupportedHistoricalKind {
                kind: resource.resource_kind.clone(),
                name: resource.resource_name.clone(),
            }
        })?;
        owner.add(
            &resource.resource_name,
            kind,
            &resource.logical_name,
            Some(&resource.pipeline_name),
            Ownership::HistoricalManifest,
        );
    }
    Ok(())
}

/// Model and source names that only manifest history attributes to affected pipelines.
fn historical_logical_names(
    project: &Project,
    snapshot: &Snapshot,
    affected: &HashSet<&str>,
) -> (Vec<String>, Vec<String>) {
    let current_models: HashSet<&str> = project
        .models
        .iter()
        .map(|&model| project.keys[model].name.as_str())
        .collect();
    let current_sources: HashSet<&str> = project
        .sources
        .iter()
        .map(|&source| project.keys[source].name.as_str())
        .collect();
    let mut latest: BTreeMap<(&str, &str), HashSet<&str>> = BTreeMap::new();
    for manifest in &snapshot.manifests {
        let mut by_key: BTreeMap<(&str, &str), HashSet<&str>> = BTreeMap::new();
        for resource in &manifest.resources {
            by_key
                .entry((&resource.logical_type, &resource.logical_name))
                .or_default()
                .insert(&resource.pipeline_name);
        }
        for (key, pipelines) in by_key {
            latest.entry(key).or_insert(pipelines);
        }
    }
    let (mut models, mut sources) = (Vec::new(), Vec::new());
    for ((logical_type, logical_name), pipelines) in latest {
        if pipelines.is_empty() || !pipelines.iter().all(|name| affected.contains(name)) {
            continue;
        }
        if logical_type == "model" && !current_models.contains(logical_name) {
            models.push(logical_name.to_owned());
        }
        if logical_type == "source" && !current_sources.contains(logical_name) {
            sources.push(logical_name.to_owned());
        }
    }
    (models, sources)
}

fn add_inventory_relations(
    owner: &mut Owner<'_>,
    inventory: &Inventory,
    database: &str,
    affected_logical: &HashSet<&str>,
    include_all: bool,
) -> Result<(), DestructionError> {
    let mut logical_name_by_published: HashMap<String, String> = HashMap::new();
    let mut deployments: Vec<&Deployment> = inventory.deployments.iter().collect();
    deployments.sort_by(|left, right| left.deployment_id.cmp(&right.deployment_id));
    for deployment in deployments {
        let mut mappings: Vec<&Mapping> = deployment.mappings.iter().collect();
        mappings.sort_by(|left, right| {
            (&left.physical_name, &left.name).cmp(&(&right.physical_name, &right.name))
        });
        for mapping in mappings {
            let logical_model_name = &mapping.logical_model_name;
            logical_name_by_published.insert(mapping.name.clone(), logical_model_name.clone());
            logical_name_by_published
                .insert(mapping.physical_name.clone(), logical_model_name.clone());
            if !include_all && !affected_logical.contains(logical_model_name.as_str()) {
                continue;
            }
            if let Some(other) = mapping
                .database
                .as_deref()
                .filter(|other| *other != database)
            {
                return Err(DestructionError::Resource(format!(
                    "Destruction cannot mutate recorded virtual resources outside the target \
                     database: {other}.{}",
                    mapping.physical_name
                )));
            }
            if is_metadata_relation(&mapping.physical_name) {
                continue;
            }
            owner.associate(
                &mapping.physical_name,
                object_type_kind(&mapping.object_type)?,
                logical_model_name,
                logical_model_name,
                Ownership::VirtualPhysicalMapping,
            );
        }
    }
    let mut events: Vec<&PublishEvent> = inventory.publish_events.iter().collect();
    events.sort_by(|left, right| {
        (&left.published_at, &left.deployment_id).cmp(&(&right.published_at, &right.deployment_id))
    });
    for event in events {
        let mut bindings: Vec<&Binding> = event.bindings.iter().collect();
        bindings.sort_by(|left, right| {
            (&left.logical_name, &left.database).cmp(&(&right.logical_name, &right.database))
        });
        for binding in bindings {
            if binding.database != database || is_metadata_relation(&binding.logical_name) {
                continue;
            }
            let logical_name: String = match owner.owned.get(&binding.logical_name) {
                Some(relation) => relation.logical_names.first().cloned().unwrap_or_default(),
                None => logical_name_by_published
                    .get(&binding.logical_name)
                    .cloned()
                    .unwrap_or_else(|| binding.logical_name.clone()),
            };
            if !include_all && !affected_logical.contains(logical_name.as_str()) {
                continue;
            }
            owner.associate(
                &binding.logical_name,
                RelationKind::View,
                &logical_name,
                &logical_name,
                Ownership::PublishedStableBinding,
            );
            if !is_metadata_relation(&binding.physical_name) {
                let physical_logical_name = logical_name_by_published
                    .get(&binding.physical_name)
                    .cloned()
                    .unwrap_or_else(|| logical_name.clone());
                owner.associate(
                    &binding.physical_name,
                    RelationKind::Table,
                    &physical_logical_name,
                    &logical_name,
                    Ownership::VirtualPhysicalMapping,
                );
            }
        }
    }
    Ok(())
}

/// `value` with thousands separators, as Python's `f"{value:,}"`.
fn with_thousands(value: i128) -> String {
    let digits = value.unsigned_abs().to_string();
    let mut grouped = String::new();
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            grouped.push(',');
        }
        grouped.push(digit);
    }
    if value < 0 {
        grouped.insert(0, '-');
    }
    grouped
}

/// Warehouse evidence for each owned relation, given `(total bytes, active parts)` per table.
///
/// # Errors
/// Rejects plans whose existing relations exceed the warehouse DROP size `limit`.
pub fn build_evidence(
    database: &str,
    owned: &[OwnedRelation],
    catalog: &[CatalogRelation],
    stats: &HashMap<String, (i128, i128)>,
    limit: Option<i128>,
) -> Result<Vec<Evidence>, DestructionError> {
    let owned_names: HashSet<&str> = owned
        .iter()
        .map(|relation| relation.name.as_str())
        .collect();
    let evidence: Vec<Evidence> = owned
        .iter()
        .map(|relation| {
            let observed = catalog
                .iter()
                .find(|candidate| candidate.name == relation.name);
            let kind = match observed.map(|observed| observed.engine.as_str()) {
                Some("View") => RelationKind::View,
                Some("MaterializedView") => RelationKind::MaterializedView,
                _ => relation.kind,
            };
            let parts = (kind == RelationKind::Table && observed.is_some())
                .then(|| stats.get(&relation.name).copied().unwrap_or((0, 0)));
            let dependency_relation_names = observed.map_or_else(Vec::new, |observed| {
                sorted_unique(
                    observed
                        .dependency_names
                        .iter()
                        .filter(|name| owned_names.contains(name.as_str())),
                )
            });
            Evidence {
                database: database.to_owned(),
                name: relation.name.clone(),
                kind,
                exists: observed.is_some(),
                total_bytes: parts.map(|(bytes, _)| bytes),
                active_parts: parts.map(|(_, parts)| parts),
                catalog_fingerprint: observed
                    .and_then(|observed| observed.ownership_generation.clone()),
                logical_names: relation.logical_names.iter().cloned().collect(),
                pipeline_names: relation.pipeline_names.iter().cloned().collect(),
                ownership: relation.ownership.iter().copied().collect(),
                dependency_relation_names,
            }
        })
        .collect();
    if let Some(limit) = limit {
        let oversized: Vec<String> = evidence
            .iter()
            .filter(|relation| relation.exists)
            .filter_map(|relation| {
                relation
                    .total_bytes
                    .filter(|bytes| *bytes > limit)
                    .map(|bytes| {
                        format!(
                            "{}.{} ({} bytes)",
                            relation.database,
                            relation.name,
                            with_thousands(bytes)
                        )
                    })
            })
            .collect();
        if !oversized.is_empty() {
            return Err(DestructionError::Resource(format!(
                "Warehouse relation DROP limit is {} bytes; oversized resources block \
                 destruction before mutation: {}",
                with_thousands(limit),
                oversized.join(", ")
            )));
        }
    }
    Ok(evidence)
}

/// Stable, ordered challenge values an operator must type to confirm destruction.
///
/// # Errors
/// Rejects an empty pipeline set, which nothing could attribute a challenge to.
pub fn build_challenges(
    pipeline_names: &[String],
    production_reset: bool,
) -> Result<Vec<String>, DestructionError> {
    let names = sorted_unique(pipeline_names);
    if names.is_empty() {
        return Err(DestructionError::Selection(
            "Destructive execution requires at least one attributable pipeline challenge"
                .to_owned(),
        ));
    }
    let mut selected = if names.len() <= MAX_NAMED_CHALLENGES {
        names
    } else {
        vec![
            names[0].clone(),
            names[names.len() / 2].clone(),
            names[names.len() - 1].clone(),
        ]
    };
    if production_reset {
        selected.push("PRODUCTION".to_owned());
    }
    Ok(selected)
}

/// The fields of a plan its fingerprint covers.
pub struct PlanIdentity<'a> {
    pub operation: Operation,
    pub target: &'a str,
    pub database: &'a str,
    pub metadata_database: &'a str,
    pub requested_pipeline_names: &'a [String],
    pub included_dependent_pipeline_names: &'a [String],
    pub affected_pipeline_names: &'a [String],
    pub affected_model_names: &'a [String],
    pub affected_source_names: &'a [String],
    pub relations: &'a [Evidence],
    pub challenges: &'a [String],
    pub manifest_fingerprint: &'a str,
    pub relation_drop_size_limit: Option<i128>,
    pub relation_drop_size_server_limit: Option<i128>,
    pub relation_drop_size_override: Option<i128>,
    pub relation_drop_size_policy_observed: bool,
    pub include_orphans: bool,
}

fn text(value: &str) -> JsonValue {
    JsonValue::String(value.to_owned())
}

fn optional_text(value: Option<&str>) -> JsonValue {
    value.map_or(JsonValue::Null, text)
}

fn texts<S: AsRef<str>>(values: &[S]) -> JsonValue {
    JsonValue::Array(values.iter().map(|value| text(value.as_ref())).collect())
}

fn integer(value: Option<i128>) -> JsonValue {
    value.map_or(JsonValue::Null, |value| {
        JsonValue::Integer(JsonInteger::from(value))
    })
}

fn object(fields: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        fields
            .into_iter()
            .map(|(key, value)| (JsonKey::Text(key.to_owned()), value))
            .collect(),
    )
}

/// The SHA-256 of the plan identity's canonical sorted, compact, ASCII-only JSON.
///
/// Byte sizes and part counts are excluded, so footprint changes alone are not drift.
#[must_use]
pub fn plan_fingerprint(plan: &PlanIdentity<'_>) -> String {
    let relations = plan
        .relations
        .iter()
        .map(|relation| {
            let ownership: Vec<&str> = relation
                .ownership
                .iter()
                .map(|value| value.as_str())
                .collect();
            object(vec![
                ("database", text(&relation.database)),
                ("name", text(&relation.name)),
                ("kind", text(relation.kind.as_str())),
                ("exists", JsonValue::Bool(relation.exists)),
                (
                    "catalog_fingerprint",
                    optional_text(relation.catalog_fingerprint.as_deref()),
                ),
                ("logical_names", texts(&relation.logical_names)),
                ("pipeline_names", texts(&relation.pipeline_names)),
                ("ownership", texts(&ownership)),
                (
                    "dependency_relation_names",
                    texts(&relation.dependency_relation_names),
                ),
            ])
        })
        .collect();
    let mut fields = vec![
        ("operation", text(plan.operation.as_str())),
        ("target", text(plan.target)),
        ("database", text(plan.database)),
        ("metadata_database", text(plan.metadata_database)),
        (
            "requested_pipeline_names",
            texts(plan.requested_pipeline_names),
        ),
        (
            "included_dependent_pipeline_names",
            texts(plan.included_dependent_pipeline_names),
        ),
        (
            "affected_pipeline_names",
            texts(plan.affected_pipeline_names),
        ),
        ("affected_model_names", texts(plan.affected_model_names)),
        ("affected_source_names", texts(plan.affected_source_names)),
        ("relations", JsonValue::Array(relations)),
        ("challenges", texts(plan.challenges)),
        ("preserves_sources", JsonValue::Bool(false)),
        ("preserves_replay_data", JsonValue::Bool(false)),
        ("manifest_fingerprint", text(plan.manifest_fingerprint)),
        (
            "relation_drop_size_limit",
            integer(plan.relation_drop_size_limit),
        ),
        (
            "relation_drop_size_server_limit",
            integer(plan.relation_drop_size_server_limit),
        ),
        (
            "relation_drop_size_override",
            integer(plan.relation_drop_size_override),
        ),
        (
            "relation_drop_size_policy_observed",
            JsonValue::Bool(plan.relation_drop_size_policy_observed),
        ),
    ];
    if plan.include_orphans {
        fields.push(("include_orphans", JsonValue::Bool(true)));
    }
    let mut options = StdlibJsonOptions::new(None).with_separators(",", ":");
    options.sort_keys = true;
    let canonical = dumps(&object(fields), &JsonDialect::Stdlib(options))
        .expect("plan identities hold only strings, integers, booleans, and nulls");
    sqltide_planner::direct::sha256_hex(&canonical)
}

/// Pipelines that manifest history owns and the compiled project no longer declares.
#[must_use]
pub fn inactive_pipelines(current: &[String], snapshot: &Snapshot) -> Vec<InactivePipeline> {
    let current: HashSet<&str> = current.iter().map(String::as_str).collect();
    let mut models: BTreeMap<&str, HashSet<&str>> = BTreeMap::new();
    let mut resources: BTreeMap<&str, HashSet<&str>> = BTreeMap::new();
    let mut published_at: HashMap<&str, &str> = HashMap::new();
    let mut seen: HashSet<&str> = HashSet::new();
    for manifest in &snapshot.manifests {
        let mut by_name: BTreeMap<&str, Vec<&ManifestResource>> = BTreeMap::new();
        for resource in &manifest.resources {
            by_name
                .entry(&resource.resource_name)
                .or_default()
                .push(resource);
        }
        for (name, named) in by_name {
            if !seen.insert(name)
                || named
                    .iter()
                    .any(|resource| current.contains(resource.pipeline_name.as_str()))
            {
                continue;
            }
            for resource in named {
                let pipeline = resource.pipeline_name.as_str();
                published_at
                    .entry(pipeline)
                    .or_insert(&manifest.published_at);
                resources.entry(pipeline).or_default().insert(name);
                if resource.logical_type == "model" {
                    models
                        .entry(pipeline)
                        .or_default()
                        .insert(&resource.logical_name);
                }
            }
        }
    }
    resources
        .into_iter()
        .map(|(name, names)| InactivePipeline {
            name: name.to_owned(),
            model_count: models.get(name).map_or(0, HashSet::len),
            resource_count: names.len(),
            last_published_at: published_at[name].to_owned(),
        })
        .collect()
}

#[cfg(test)]
mod tests;
