//! Canonical project manifest resources and their deterministic fingerprint.

use sha2::{Digest, Sha256};
use sqltide_json::{JsonDialect, JsonKey, JsonValue, StdlibJsonOptions, dumps};

const SOURCE_LOGICAL_TYPE: &str = "source";

/// The adapter resource kinds a manifest records.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManifestResourceKind {
    ManagedSource,
    Table,
    View,
    MaterializedView,
}

impl ManifestResourceKind {
    /// The kind from its manifest name.
    #[must_use]
    pub fn parse(kind: &str) -> Option<Self> {
        match kind {
            "managed_source" => Some(Self::ManagedSource),
            "table" => Some(Self::Table),
            "view" => Some(Self::View),
            "materialized_view" => Some(Self::MaterializedView),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::ManagedSource => "managed_source",
            Self::Table => "table",
            Self::View => "view",
            Self::MaterializedView => "materialized_view",
        }
    }
}

/// One logical resource, the pipelines that own or read it, and its realized resources.
pub struct ManifestLogicalResource {
    pub logical_type: String,
    pub logical_name: String,
    pub pipeline_names: Vec<String>,
    /// `(kind, resource name)` in realization order.
    pub resources: Vec<(ManifestResourceKind, String)>,
}

/// `(pipeline, logical type, logical name, role, database, resource name, kind)`.
pub type ManifestResourceIdentity = [String; 7];

/// The manifest's sorted pipelines, sorted resource identities, and fingerprint.
pub struct ManifestContents {
    pub pipelines: Vec<String>,
    pub resources: Vec<ManifestResourceIdentity>,
    pub fingerprint: String,
}

/// The project-level facts the manifest fingerprint covers.
pub struct ManifestTarget<'a> {
    pub project_identity: &'a str,
    pub target_name: &'a str,
    pub target_database: &'a str,
    pub is_production: bool,
}

fn resource_role(logical_type: &str, kind: ManifestResourceKind) -> &'static str {
    if logical_type == SOURCE_LOGICAL_TYPE {
        return match kind {
            ManifestResourceKind::ManagedSource => "source",
            ManifestResourceKind::Table => "landing",
            ManifestResourceKind::View | ManifestResourceKind::MaterializedView => "landing_view",
        };
    }
    match kind {
        ManifestResourceKind::MaterializedView => "materialization",
        _ => "primary",
    }
}

/// One record per (resource, owning pipeline) in identity order, plus the distinct pipeline
/// names and the fingerprint over both and the target.
#[must_use]
pub fn manifest_contents(
    target: &ManifestTarget<'_>,
    pipeline_names: Vec<String>,
    logical_resources: &[ManifestLogicalResource],
) -> ManifestContents {
    let mut pipelines: Vec<String> = pipeline_names;
    pipelines.sort_unstable();
    pipelines.dedup();
    let mut resources: Vec<ManifestResourceIdentity> = Vec::new();
    for logical in logical_resources {
        for (kind, resource_name) in &logical.resources {
            for pipeline_name in &logical.pipeline_names {
                resources.push([
                    pipeline_name.clone(),
                    logical.logical_type.clone(),
                    logical.logical_name.clone(),
                    resource_role(&logical.logical_type, *kind).to_owned(),
                    target.target_database.to_owned(),
                    resource_name.clone(),
                    kind.name().to_owned(),
                ]);
            }
        }
    }
    resources.sort();
    let fingerprint: String = fingerprint(target, &pipelines, &resources);
    ManifestContents {
        pipelines,
        resources,
        fingerprint,
    }
}

fn texts<'a>(values: impl IntoIterator<Item = &'a String>) -> JsonValue {
    JsonValue::Array(values.into_iter().cloned().map(JsonValue::String).collect())
}

fn fingerprint(
    target: &ManifestTarget<'_>,
    pipelines: &[String],
    resources: &[ManifestResourceIdentity],
) -> String {
    let payload = JsonValue::Object(
        [
            ("is_production", JsonValue::Bool(target.is_production)),
            ("pipelines", texts(pipelines)),
            (
                "project_identity",
                JsonValue::String(target.project_identity.to_owned()),
            ),
            (
                "resources",
                JsonValue::Array(resources.iter().map(texts).collect()),
            ),
            (
                "target_database",
                JsonValue::String(target.target_database.to_owned()),
            ),
            (
                "target_name",
                JsonValue::String(target.target_name.to_owned()),
            ),
        ]
        .into_iter()
        .map(|(key, value)| (JsonKey::from(key.to_owned()), value))
        .collect(),
    );
    let mut options = StdlibJsonOptions::new(None).with_separators(",", ":");
    options.sort_keys = true;
    let canonical: String = dumps(&payload, &JsonDialect::Stdlib(options))
        .expect("manifest payloads hold only strings, booleans, and arrays");
    format!("{:x}", Sha256::digest(canonical.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_each_owning_pipeline_in_identity_order() {
        let target = ManifestTarget {
            project_identity: "p",
            target_name: "",
            target_database: "db",
            is_production: false,
        };
        let contents = manifest_contents(
            &target,
            vec!["b".to_owned(), "a".to_owned(), "b".to_owned()],
            &[ManifestLogicalResource {
                logical_type: "source".to_owned(),
                logical_name: "orders".to_owned(),
                pipeline_names: vec!["b".to_owned(), "a".to_owned()],
                resources: vec![(
                    ManifestResourceKind::MaterializedView,
                    "mv__orders".to_owned(),
                )],
            }],
        );
        assert_eq!(contents.pipelines, ["a", "b"]);
        assert_eq!(contents.resources[0][0], "a");
        assert_eq!(contents.resources[0][3], "landing_view");
        assert_eq!(contents.fingerprint.len(), 64);
    }
}
