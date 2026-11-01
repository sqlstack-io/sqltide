//! Authored model relation names and project-wide physical relation name claims.

use std::collections::HashMap;
use std::collections::hash_map::Entry;

use sqltide_discovery::config::{DEFAULT_TABLE_PREFIX, DEFAULT_VIEW_PREFIX};

pub const KAFKA_TABLE_NAME_PREFIX: &str = "kafka__";
pub const RAW_TABLE_NAME_PREFIX: &str = "raw__";
pub const MATERIALIZED_VIEW_NAME_PREFIX: &str = "mv__";

const RESERVED_MODEL_RELATION_PREFIXES: [&str; 3] = [
    KAFKA_TABLE_NAME_PREFIX,
    RAW_TABLE_NAME_PREFIX,
    MATERIALIZED_VIEW_NAME_PREFIX,
];

/// Whether a model materializes as a table or a view.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ModelRelationKind {
    Table,
    View,
}

/// One authored model's naming inputs, before prefix resolution.
pub struct ModelNaming<'a> {
    pub logical_name: &'a str,
    pub kind: ModelRelationKind,
    pub exact_name: Option<&'a str>,
    pub pipeline_prefix: Option<&'a str>,
    pub project_prefix: Option<&'a str>,
}

/// A compiled source as it claims physical relation names.
pub enum SourceClaim<'a> {
    /// A source whose relations SQLTide generates: Kafka table, raw table, landing view.
    Managed { name: &'a str },
    /// An externally owned table adopted as a source.
    Adopted { name: &'a str, table_name: &'a str },
}

/// A compiled model as it claims physical relation names.
pub struct ModelClaim<'a> {
    pub name: &'a str,
    pub relation_name: &'a str,
    pub kind: ModelRelationKind,
}

/// The model's relation name: exact, else pipeline, project, then built-in prefix plus its name.
///
/// # Errors
///
/// Returns the compile error message when the name is not a plain identifier, uses a
/// framework-owned prefix, or ends like a deployment-suffixed physical name.
pub fn resolve_model_relation_name(model: &ModelNaming<'_>) -> Result<String, String> {
    let relation_name: String = match model.exact_name {
        Some(exact_name) => exact_name.to_owned(),
        None => {
            let built_in_prefix: &str = match model.kind {
                ModelRelationKind::Table => DEFAULT_TABLE_PREFIX,
                ModelRelationKind::View => DEFAULT_VIEW_PREFIX,
            };
            let prefix: &str = model
                .pipeline_prefix
                .or(model.project_prefix)
                .unwrap_or(built_in_prefix);
            format!("{prefix}{}", model.logical_name)
        }
    };
    validate_model_relation_name(model.logical_name, &relation_name)?;
    Ok(relation_name)
}

/// Reject two owners of one physical relation name.
///
/// Claims run in a fixed order, which decides the owner each message names: generated source
/// relations, then models and their generated materialized views, then adopted source tables
/// (the first adopting source per table name).
///
/// # Errors
///
/// Returns the compile error message naming the first collision.
pub fn validate_project_relation_names(
    sources: &[SourceClaim<'_>],
    models: &[ModelClaim<'_>],
) -> Result<(), String> {
    let mut owners: HashMap<String, String> = HashMap::new();
    let mut adopted: Vec<(&str, String)> = Vec::new();
    for source in sources {
        match source {
            SourceClaim::Adopted { name, table_name } => {
                if adopted.iter().all(|(claimed, _)| claimed != table_name) {
                    adopted.push((table_name, format!("adopted source '{name}'")));
                }
            }
            SourceClaim::Managed { name } => {
                for (prefix, role) in [
                    (KAFKA_TABLE_NAME_PREFIX, "Kafka table"),
                    (RAW_TABLE_NAME_PREFIX, "raw table"),
                    (MATERIALIZED_VIEW_NAME_PREFIX, "landing materialized view"),
                ] {
                    claim(
                        &mut owners,
                        format!("{prefix}{name}"),
                        format!("generated {role} for source '{name}'"),
                    )?;
                }
            }
        }
    }
    for model in models {
        claim(
            &mut owners,
            model.relation_name.to_owned(),
            format!("model '{}'", model.name),
        )?;
        if model.kind == ModelRelationKind::Table {
            claim(
                &mut owners,
                format!("{MATERIALIZED_VIEW_NAME_PREFIX}{}", model.name),
                format!("generated materialized view for model '{}'", model.name),
            )?;
        }
    }
    for (table_name, owner) in adopted {
        claim(&mut owners, table_name.to_owned(), owner)?;
    }
    Ok(())
}

fn claim(
    owners: &mut HashMap<String, String>,
    relation_name: String,
    owner: String,
) -> Result<(), String> {
    match owners.entry(relation_name) {
        Entry::Occupied(existing) => Err(format!(
            "Relation name '{}' is used by both {} and {owner}",
            existing.key(),
            existing.get()
        )),
        Entry::Vacant(slot) => {
            slot.insert(owner);
            Ok(())
        }
    }
}

fn validate_model_relation_name(logical_name: &str, relation_name: &str) -> Result<(), String> {
    if !is_unqualified_identifier(relation_name) {
        return Err(format!(
            "Model '{logical_name}' resolves to invalid relation name '{relation_name}'; \
             expected a non-empty unqualified identifier"
        ));
    }
    if RESERVED_MODEL_RELATION_PREFIXES
        .iter()
        .any(|prefix| relation_name.starts_with(prefix))
    {
        return Err(format!(
            "Model '{logical_name}' relation name '{relation_name}' uses reserved prefix; \
             kafka__, raw__, and mv__ are framework-owned"
        ));
    }
    if has_deployment_suffix(relation_name) {
        return Err(format!(
            "Model '{logical_name}' relation name '{relation_name}' looks like a fixed \
             deployment-suffixed physical name"
        ));
    }
    Ok(())
}

/// `[A-Za-z_][A-Za-z0-9_]*`.
fn is_unqualified_identifier(name: &str) -> bool {
    let mut bytes = name.bytes();
    bytes
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

/// Ends in `__YYYYMMDDTHHMMSSZ_<6 lowercase hex>` or `__reconcile_YYYYMMDDTHHMMSSZ`.
///
/// Only called on ASCII identifiers, so digits are ASCII digits.
fn has_deployment_suffix(name: &str) -> bool {
    let timestamp = |text: &[u8]| {
        text.len() == 16
            && text[..8].iter().all(u8::is_ascii_digit)
            && text[8] == b'T'
            && text[9..15].iter().all(u8::is_ascii_digit)
            && text[15] == b'Z'
    };
    let bytes: &[u8] = name.as_bytes();
    let suffixed = |marker: &[u8], length: usize| {
        bytes.len() >= marker.len() + length
            && bytes[bytes.len() - length - marker.len()..].starts_with(marker)
    };
    let deployment: bool = suffixed(b"__", 23) && {
        let tail: &[u8] = &bytes[bytes.len() - 23..];
        timestamp(&tail[..16])
            && tail[16] == b'_'
            && tail[17..]
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    };
    deployment || (suffixed(b"__reconcile_", 16) && timestamp(&bytes[bytes.len() - 16..]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn naming(exact_name: Option<&str>) -> ModelNaming<'_> {
        ModelNaming {
            logical_name: "orders",
            kind: ModelRelationKind::Table,
            exact_name,
            pipeline_prefix: None,
            project_prefix: Some("p_"),
        }
    }

    #[test]
    fn resolves_prefix_precedence_and_rejects_reserved_names() {
        assert_eq!(
            resolve_model_relation_name(&naming(None)),
            Ok("p_orders".to_owned())
        );
        assert_eq!(
            resolve_model_relation_name(&ModelNaming {
                pipeline_prefix: Some(""),
                ..naming(None)
            }),
            Ok("orders".to_owned())
        );
        for rejected in [
            "",
            "9orders",
            "or-ders",
            "ordérs",
            "mv__orders",
            "orders__20260131T120000Z_ab12cd",
            "orders__reconcile_20260131T120000Z",
        ] {
            assert!(
                resolve_model_relation_name(&naming(Some(rejected))).is_err(),
                "{rejected}"
            );
        }
        for accepted in [
            "orders__20260131T120000Z_AB12CD",
            "orders_20260131T120000Z_ab12cd",
        ] {
            assert!(
                resolve_model_relation_name(&naming(Some(accepted))).is_ok(),
                "{accepted}"
            );
        }
    }

    #[test]
    fn names_generated_owner_before_adopted_owner() {
        let sources = [
            SourceClaim::Adopted {
                name: "legacy",
                table_name: "raw__events",
            },
            SourceClaim::Managed { name: "events" },
        ];
        assert_eq!(
            validate_project_relation_names(&sources, &[]),
            Err(
                "Relation name 'raw__events' is used by both generated raw table for source \
                 'events' and adopted source 'legacy'"
                    .to_owned()
            )
        );
    }
}
