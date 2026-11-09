//! The project access policy: strict YAML in `access.yml`, validated into normalized roles and
//! fingerprinted independently of authored order.

use std::collections::{BTreeSet, HashSet};

use sha2::{Digest, Sha256};
use sqltide_json::{JsonDialect, JsonKey, JsonValue, StdlibJsonOptions, dumps};
use sqltide_sqltext::python_text::python_strip;

use crate::yaml::composer::{Composer, MERGE_TAG, Node, NodeValue};
use crate::yaml::constructor::construct_document;
use crate::yaml::parser::{Event, EventKind};
use crate::yaml::{Value, YamlError, parser};

/// The operations SQLTide authorizes, by their authored names.
pub const PERMISSIONS: [&str; 15] = [
    "project.reload",
    "source.messages.read",
    "quality.test.run",
    "quality.audit.run",
    "build.direct.run",
    "deployment.create",
    "build.cancel",
    "build.kill",
    "deployment.promote",
    "deployment.cleanup",
    "pipeline.destroy",
    "target.reset",
    "automation.manage",
    "account.manage",
    "role.assign",
];
/// Permissions only built-in roles hold.
pub const SYSTEM_ONLY_PERMISSIONS: [&str; 2] = ["account.manage", "role.assign"];
/// Permissions a grant may give on named pipelines.
pub const PIPELINE_PERMISSIONS: [&str; 7] = [
    "quality.test.run",
    "quality.audit.run",
    "build.direct.run",
    "deployment.create",
    "build.cancel",
    "deployment.promote",
    "pipeline.destroy",
];
/// Permissions a grant may give at project scope.
pub const PROJECT_PERMISSIONS: [&str; 5] = [
    "project.reload",
    "source.messages.read",
    "quality.test.run",
    "quality.audit.run",
    "automation.manage",
];
/// Permissions a grant may give at target scope.
pub const TARGET_PERMISSIONS: [&str; 5] = [
    "source.messages.read",
    "build.kill",
    "deployment.cleanup",
    "target.reset",
    "automation.manage",
];
const PROTECTED_ROLE_NAMES: [&str; 2] = ["admin", "viewer"];
const TOP_LEVEL_KEYS: [&str; 1] = ["roles"];
const ROLE_KEYS: [&str; 2] = ["description", "grants"];
const GRANT_KEYS: [&str; 3] = ["pipelines", "scope", "permissions"];
const SCOPES: [&str; 2] = ["project", "target"];

/// One grant: permissions on named pipelines, or at `project` or `target` scope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccessGrant {
    /// Sorted permission names.
    pub permissions: Vec<&'static str>,
    /// Sorted pipeline names; empty for a scoped grant.
    pub pipelines: Vec<String>,
    pub scope: Option<&'static str>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccessRole {
    pub name: String,
    pub description: Option<String>,
    /// Grants in a stable order.
    pub grants: Vec<AccessGrant>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccessPolicy {
    /// Roles sorted by name.
    pub roles: Vec<AccessRole>,
    pub fingerprint: String,
}

/// An unsafe or invalid policy, at a one-based line and column of `access.yml`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccessPolicyError {
    pub message: String,
    pub line: usize,
    pub column: usize,
}

type Compiled<T> = Result<T, AccessPolicyError>;

fn fail<T>(message: String) -> Compiled<T> {
    Err(AccessPolicyError {
        message,
        line: 1,
        column: 1,
    })
}

impl From<YamlError> for AccessPolicyError {
    fn from(error: YamlError) -> Self {
        Self {
            message: error.problem,
            line: error.mark.line + 1,
            column: error.mark.column + 1,
        }
    }
}

/// Compile `access.yml` into normalized roles for the project's pipelines.
pub fn compile_access_policy(
    contents: &str,
    pipeline_names: &HashSet<String>,
) -> Compiled<AccessPolicy> {
    let document: Value = load_strict_yaml(contents)?;
    let roles: Vec<AccessRole> = build_roles(&document, pipeline_names)?;
    let fingerprint: String = fingerprint(&roles);
    Ok(AccessPolicy { roles, fingerprint })
}

fn strict_error(problem: &str, event: &Event) -> YamlError {
    YamlError::marked(problem, event.start)
}

/// One safely loaded document, rejecting aliases, anchors, merge keys, non-scalar keys and
/// duplicate keys, which make a security policy ambiguous.
fn load_strict_yaml(contents: &str) -> Result<Value, YamlError> {
    let mut parser = parser(contents)?;
    let mut events: Vec<Event> = Vec::new();
    let mut documents: usize = 0;
    while let Some(event) = parser.next_event()? {
        match event.kind {
            EventKind::DocumentStart => {
                documents += 1;
                if documents > 1 {
                    return Err(strict_error(
                        "multiple YAML documents are not allowed",
                        &event,
                    ));
                }
            }
            EventKind::Alias => {
                return Err(strict_error("YAML aliases are not allowed", &event));
            }
            EventKind::Scalar { .. }
            | EventKind::SequenceStart { .. }
            | EventKind::MappingStart { .. }
                if event.anchor.is_some() =>
            {
                return Err(strict_error("YAML anchors are not allowed", &event));
            }
            _ => {}
        }
        events.push(event);
    }
    let Some(node) = Composer::new(events.into_iter()).single_node()? else {
        return Ok(Value::None);
    };
    validate_node(&node)?;
    construct_document(&node)
}

fn validate_node(node: &Node) -> Result<(), YamlError> {
    match &node.value {
        NodeValue::Mapping(pairs) => {
            let mut seen: HashSet<(&str, &str)> = HashSet::new();
            for (key, value) in pairs {
                if key.tag == MERGE_TAG {
                    return Err(YamlError::marked(
                        "YAML merge keys are not allowed",
                        key.start,
                    ));
                }
                let NodeValue::Scalar(text) = &key.value else {
                    return Err(YamlError::marked(
                        "mapping keys must be scalar values",
                        key.start,
                    ));
                };
                if !seen.insert((&key.tag, text)) {
                    return Err(YamlError::marked(
                        format!("duplicate mapping key '{text}'"),
                        key.start,
                    ));
                }
                validate_node(value)?;
            }
            Ok(())
        }
        NodeValue::Sequence(items) => items.iter().try_for_each(validate_node),
        NodeValue::Scalar(_) => Ok(()),
    }
}

/// A `dict` whose keys are all strings, as `(key, value)` pairs.
fn string_mapping<'a>(value: &'a Value, context: &str) -> Compiled<Vec<(&'a str, &'a Value)>> {
    let Value::Dict(entries) = value else {
        return fail(format!("{context} must be a string-keyed mapping"));
    };
    entries
        .iter()
        .map(|(key, value)| match key {
            Value::Str(key) => Ok((key.as_str(), value)),
            _ => fail(format!("{context} must be a string-keyed mapping")),
        })
        .collect()
}

fn get<'a>(entries: &[(&str, &'a Value)], key: &str) -> Option<&'a Value> {
    entries
        .iter()
        .find(|(name, _)| *name == key)
        .map(|(_, value)| *value)
}

fn reject_unknown_keys(
    entries: &[(&str, &Value)],
    allowed: &[&str],
    context: &str,
) -> Compiled<()> {
    let unknown: BTreeSet<&str> = entries
        .iter()
        .map(|(key, _)| *key)
        .filter(|key| !allowed.contains(key))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    fail(format!(
        "{context} contains unknown key(s): {}",
        unknown.into_iter().collect::<Vec<_>>().join(", ")
    ))
}

fn sequence<'a>(value: Option<&'a Value>, context: &str) -> Compiled<&'a [Value]> {
    match value {
        Some(Value::List(items) | Value::Tuple(items)) => Ok(items),
        _ => fail(format!("{context} must be a sequence")),
    }
}

fn is_blank(text: &str) -> bool {
    python_strip(&text.chars().collect::<Vec<char>>()).is_empty()
}

fn unique_strings<'a>(value: Option<&'a Value>, context: &str) -> Compiled<Vec<&'a str>> {
    let mut strings: Vec<&str> = Vec::new();
    for item in sequence(value, context)? {
        match item {
            Value::Str(text) if !is_blank(text) => strings.push(text),
            _ => return fail(format!("{context} must contain non-empty strings")),
        }
    }
    if strings.iter().collect::<HashSet<_>>().len() != strings.len() {
        return fail(format!("{context} contains duplicate values"));
    }
    if strings.is_empty() {
        return fail(format!("{context} must not be empty"));
    }
    Ok(strings)
}

fn build_roles(document: &Value, pipeline_names: &HashSet<String>) -> Compiled<Vec<AccessRole>> {
    let root: Vec<(&str, &Value)> = string_mapping(document, "access policy")?;
    reject_unknown_keys(&root, &TOP_LEVEL_KEYS, "access policy")?;
    let mut roles: Vec<(&str, &Value)> =
        string_mapping(get(&root, "roles").unwrap_or(&Value::None), "roles")?;
    roles.sort_by_key(|(name, _)| *name);
    roles
        .into_iter()
        .map(|(name, value)| build_role(name, value, pipeline_names))
        .collect()
}

fn build_role(name: &str, value: &Value, pipeline_names: &HashSet<String>) -> Compiled<AccessRole> {
    let characters: Vec<char> = name.chars().collect();
    let stripped: &[char] = python_strip(&characters);
    if stripped.is_empty() || stripped.len() != characters.len() {
        return fail(
            "role names must be non-empty and must not contain surrounding whitespace".to_owned(),
        );
    }
    if PROTECTED_ROLE_NAMES.contains(&name) {
        return fail(format!(
            "role '{name}' is protected and cannot be declared in access.yml"
        ));
    }
    let context: String = format!("role '{name}'");
    let role: Vec<(&str, &Value)> = string_mapping(value, &context)?;
    reject_unknown_keys(&role, &ROLE_KEYS, &context)?;
    let description: Option<String> = match get(&role, "description") {
        None | Some(Value::None) => None,
        Some(Value::Str(text)) if !is_blank(text) => Some(text.clone()),
        Some(_) => {
            return fail(format!("{context} description must be a non-empty string"));
        }
    };
    let grants: &[Value] = sequence(get(&role, "grants"), &format!("{context} grants"))?;
    if grants.is_empty() {
        return fail(format!("{context} must contain at least one grant"));
    }
    let mut compiled: Vec<AccessGrant> = grants
        .iter()
        .enumerate()
        .map(|(index, grant)| build_grant(&context, index, grant, pipeline_names))
        .collect::<Compiled<_>>()?;
    compiled.sort_by(|left, right| grant_sort_key(left).cmp(&grant_sort_key(right)));
    Ok(AccessRole {
        name: name.to_owned(),
        description,
        grants: compiled,
    })
}

fn grant_sort_key(grant: &AccessGrant) -> (&str, &[String], &[&'static str]) {
    (
        grant.scope.unwrap_or("pipelines"),
        &grant.pipelines,
        &grant.permissions,
    )
}

fn permission_list(permissions: &[&str]) -> String {
    permissions.join(", ")
}

fn build_grant(
    role_context: &str,
    index: usize,
    value: &Value,
    pipeline_names: &HashSet<String>,
) -> Compiled<AccessGrant> {
    let context: String = format!("{role_context} grant {}", index + 1);
    let grant: Vec<(&str, &Value)> = string_mapping(value, &context)?;
    reject_unknown_keys(&grant, &GRANT_KEYS, &context)?;
    let pipelines_value: Option<&Value> = get(&grant, "pipelines");
    let scope_value: Option<&Value> = get(&grant, "scope");
    if pipelines_value.is_some() == scope_value.is_some() {
        return fail(format!(
            "{context} must contain exactly one of 'pipelines' or 'scope'"
        ));
    }
    let permissions: Vec<&'static str> = permissions(get(&grant, "permissions"), &context)?;
    if let Some(pipelines_value) = pipelines_value {
        let mut pipelines: Vec<&str> =
            unique_strings(Some(pipelines_value), &format!("{context} pipelines"))?;
        let unknown: BTreeSet<&str> = pipelines
            .iter()
            .copied()
            .filter(|pipeline| !pipeline_names.contains(*pipeline))
            .collect();
        if !unknown.is_empty() {
            return fail(format!(
                "{context} references unknown pipelines: {}",
                unknown.into_iter().collect::<Vec<_>>().join(", ")
            ));
        }
        let invalid: Vec<&str> = permissions
            .iter()
            .copied()
            .filter(|permission| !PIPELINE_PERMISSIONS.contains(permission))
            .collect();
        if !invalid.is_empty() {
            return fail(format!(
                "{context} cannot grant pipeline permission(s): {}",
                permission_list(&invalid)
            ));
        }
        pipelines.sort_unstable();
        return Ok(AccessGrant {
            permissions,
            pipelines: pipelines.into_iter().map(str::to_owned).collect(),
            scope: None,
        });
    }
    let scope: &'static str = match scope_value {
        Some(Value::Str(text)) => match SCOPES.iter().find(|scope| **scope == text.as_str()) {
            Some(scope) => scope,
            None => {
                return fail(format!(
                    "{context} scope must be 'project' or 'target', received '{text}'"
                ));
            }
        },
        _ => return fail(format!("{context} scope must be 'project' or 'target'")),
    };
    let allowed: &[&str] = if scope == "project" {
        &PROJECT_PERMISSIONS
    } else {
        &TARGET_PERMISSIONS
    };
    let invalid: Vec<&str> = permissions
        .iter()
        .copied()
        .filter(|permission| !allowed.contains(permission))
        .collect();
    if !invalid.is_empty() {
        return fail(format!(
            "{context} cannot grant {} at {scope} scope",
            permission_list(&invalid)
        ));
    }
    Ok(AccessGrant {
        permissions,
        pipelines: Vec::new(),
        scope: Some(scope),
    })
}

fn permissions(value: Option<&Value>, context: &str) -> Compiled<Vec<&'static str>> {
    let mut permissions: Vec<&'static str> = Vec::new();
    for name in unique_strings(value, &format!("{context} permissions"))? {
        let Some(permission) = PERMISSIONS.iter().find(|permission| **permission == name) else {
            return fail(format!("{context} contains unknown permission '{name}'"));
        };
        if SYSTEM_ONLY_PERMISSIONS.contains(permission) {
            return fail(format!(
                "{context} cannot grant system-only permission '{permission}'"
            ));
        }
        permissions.push(permission);
    }
    permissions.sort_unstable();
    Ok(permissions)
}

fn json_strings<S: AsRef<str>>(values: &[S]) -> JsonValue {
    JsonValue::Array(
        values
            .iter()
            .map(|value| JsonValue::String(value.as_ref().to_owned()))
            .collect(),
    )
}

fn json_object(entries: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (JsonKey::Text(key.to_owned()), value))
            .collect(),
    )
}

fn optional_string(value: Option<&str>) -> JsonValue {
    value.map_or(JsonValue::Null, |text| JsonValue::String(text.to_owned()))
}

/// SHA-256 of the roles as sorted-key, compact, ASCII-only JSON.
fn fingerprint(roles: &[AccessRole]) -> String {
    let payload: JsonValue = JsonValue::Array(
        roles
            .iter()
            .map(|role| {
                let grants: Vec<JsonValue> = role
                    .grants
                    .iter()
                    .map(|grant| {
                        json_object(vec![
                            ("scope", optional_string(grant.scope)),
                            ("pipelines", json_strings(&grant.pipelines)),
                            ("permissions", json_strings(&grant.permissions)),
                        ])
                    })
                    .collect();
                json_object(vec![
                    ("name", JsonValue::String(role.name.clone())),
                    ("description", optional_string(role.description.as_deref())),
                    ("grants", JsonValue::Array(grants)),
                ])
            })
            .collect(),
    );
    let options: StdlibJsonOptions = StdlibJsonOptions {
        sort_keys: true,
        ..StdlibJsonOptions::new(None).with_separators(",", ":")
    };
    let canonical: String =
        dumps(&payload, &JsonDialect::Stdlib(options)).expect("the policy holds only text");
    Sha256::digest(canonical.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
