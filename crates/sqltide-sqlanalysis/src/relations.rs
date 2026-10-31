//! Resolve `__ref(...)` and `__source(...)` relations to physical targets and qualify relations
//! with an adapter database placeholder.

use std::collections::HashMap;

use serde_json::{Value, json};

use crate::facts::at;
use crate::model::ParsedModel;
use crate::tree::{
    AnalysisError, AnalysisResult, Object, SqlDialect, identifier_name, object, text, with_ctes,
};

const SOURCE_REFERENCE_FUNCTION: &str = "__source";
const MODEL_REFERENCE_FUNCTION: &str = "__ref";
const REFERENCE_TYPE_KEYWORD: &str = "ref_type";
const REFERENCE_TYPES: [&str; 2] = ["reference", "mutable"];
const REFERENCE_WITH_TYPE_ARGUMENT_COUNT: usize = 2;

/// Looks up the physical target a logical relation name resolves to.
pub type Resolver<'a> = &'a dyn Fn(&str) -> Option<String>;

/// `(relation type, name, ref type)`, as authored or as found in a relation position.
type Identity = (&'static str, String, Option<String>);

/// One reference the lexical scan found, with its character span in the authored SQL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExpectedReference {
    pub relation_type: String,
    pub name: String,
    pub ref_type: Option<String>,
    pub start: usize,
    pub end: usize,
}

/// A model query in canonical form and with its relations qualified by the database placeholder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedQuery {
    pub canonical_sql: String,
    pub database_template: String,
}

#[derive(Debug)]
struct CachedRelation {
    tree: Value,
    canonical: Option<String>,
    templates: HashMap<String, String>,
}

/// Parsed and generated resolution targets, shared by every model of one compiler invocation.
#[derive(Debug, Default)]
pub struct RelationCache {
    relations: HashMap<String, CachedRelation>,
    /// How many targets were parsed, which repeated targets must not increase.
    pub parses: usize,
    /// How many relations were generated, which repeated targets must not increase.
    pub generations: usize,
}

impl RelationCache {
    fn relation(
        &mut self,
        target: &str,
        dialect: &SqlDialect,
    ) -> AnalysisResult<&mut CachedRelation> {
        if !self.relations.contains_key(target) {
            self.parses += 1;
            let tree: Value = parse_relation(target, dialect)?;
            self.relations.insert(
                target.to_owned(),
                CachedRelation {
                    tree,
                    canonical: None,
                    templates: HashMap::new(),
                },
            );
        }
        Ok(self
            .relations
            .get_mut(target)
            .expect("the relation was cached above"))
    }

    fn canonical(&mut self, target: &str, dialect: &SqlDialect) -> AnalysisResult<String> {
        let relation: &mut CachedRelation = self.relation(target, dialect)?;
        if let Some(canonical) = &relation.canonical {
            return Ok(canonical.clone());
        }
        let canonical: String = dialect.generate(&relation.tree, false)?;
        relation.canonical = Some(canonical.clone());
        self.generations += 1;
        Ok(canonical)
    }

    fn template(
        &mut self,
        target: &str,
        dialect: &SqlDialect,
        database_placeholder: &str,
    ) -> AnalysisResult<String> {
        let relation: &mut CachedRelation = self.relation(target, dialect)?;
        if let Some(template) = relation.templates.get(database_placeholder) {
            return Ok(template.clone());
        }
        let mut tree: Value = relation.tree.clone();
        qualify_relations(&mut tree, database_placeholder, &[]);
        let template: String = dialect.generate(&tree, false)?;
        relation
            .templates
            .insert(database_placeholder.to_owned(), template.clone());
        self.generations += 1;
        Ok(template)
    }

    /// The resolution target for one relation expression, caching its parsed relation.
    fn reference_target(
        &mut self,
        expression: &Value,
        dialect: &SqlDialect,
        resolver: Resolver<'_>,
        identities: &mut Vec<Identity>,
    ) -> AnalysisResult<Option<String>> {
        let Some(entries) = expression.as_object() else {
            return Ok(None);
        };
        if let Some(alias) = object(entries.get("alias")) {
            return self.reference_target(
                alias.get("this").unwrap_or(&Value::Null),
                dialect,
                resolver,
                identities,
            );
        }
        let Some(identity) = reference_identity(entries) else {
            return Ok(None);
        };
        let Some(target) = resolver(&identity.1) else {
            return Err(AnalysisError(format!("Unresolved ref: {}", identity.1)));
        };
        identities.push(identity);
        self.relation(&target, dialect)?;
        Ok(Some(target))
    }

    /// Resolve and qualify one analyzed model without reparsing it.
    ///
    /// # Errors
    ///
    /// Returns the first failure the compiler reports: a raw physical relation, the first
    /// unresolvable relation position, a reference outside a relation position, or the reserved
    /// database placeholder appearing in the query.
    pub fn resolve(
        &mut self,
        model: &ParsedModel,
        authored_sql: &str,
        references: &[ExpectedReference],
        resolver: Resolver<'_>,
        dialect: &SqlDialect,
        database_placeholder: &str,
    ) -> AnalysisResult<ResolvedQuery> {
        if let Some(raw_relation) = &model.raw_relation {
            return Err(AnalysisError(format!(
                "Model relation '{raw_relation}' must be referenced via __ref(...) or __source(...)"
            )));
        }
        let mut identities: Vec<Identity> = Vec::new();
        let mut deferred: Option<AnalysisError> = None;
        for slot in &model.reference_slots {
            let expression: &Value = at(&model.tree, slot).unwrap_or(&Value::Null);
            let mut found: Vec<Identity> = Vec::new();
            match self.reference_target(expression, dialect, resolver, &mut found) {
                Ok(_) => identities.extend(found),
                Err(error) => {
                    deferred.get_or_insert(error);
                }
            }
        }
        if let Some(error) = deferred {
            return Err(error);
        }
        validate_reference_identities(references, identities)?;
        let mut ordered: Vec<&ExpectedReference> = references.iter().collect();
        ordered.sort_by_key(|reference| reference.start);
        let authored: Vec<char> = authored_sql.chars().collect();
        let canonical_sql: String =
            self.substituted(&authored, &ordered, resolver, |cache, target| {
                cache.canonical(target, dialect)
            })?;
        reject_reserved_database_placeholder(&canonical_sql, database_placeholder)?;
        reject_reserved_database_placeholder(authored_sql, database_placeholder)?;
        let database_template: String =
            self.substituted(&authored, &ordered, resolver, |cache, target| {
                cache.template(target, dialect, database_placeholder)
            })?;
        Ok(ResolvedQuery {
            canonical_sql,
            database_template,
        })
    }

    /// The authored SQL with each reference span replaced by its rendered target relation.
    fn substituted(
        &mut self,
        authored: &[char],
        references: &[&ExpectedReference],
        resolver: Resolver<'_>,
        mut render: impl FnMut(&mut Self, &str) -> AnalysisResult<String>,
    ) -> AnalysisResult<String> {
        let mut pieces: String = String::with_capacity(authored.len());
        let mut cursor: usize = 0;
        for reference in references {
            let Some(target) = resolver(&reference.name) else {
                return Err(AnalysisError(format!("Unresolved ref: {}", reference.name)));
            };
            let relation: String = render(self, &target)?;
            pieces
                .extend(&authored[cursor.min(authored.len())..reference.start.min(authored.len())]);
            pieces.push_str(&relation);
            cursor = reference.end;
        }
        pieces.extend(&authored[cursor.min(authored.len())..]);
        Ok(pieces)
    }
}

/// Validate one relation-only rewrite and return the canonical relation for each referenced target.
///
/// # Errors
///
/// Returns the first unresolvable relation, a reference outside a relation position, or a
/// rewritten query that cannot be generated.
pub fn validated_relation_rewrite(
    sql: &str,
    references: &[ExpectedReference],
    resolver: Resolver<'_>,
    cache: &mut RelationCache,
    dialect: &SqlDialect,
) -> AnalysisResult<Vec<(String, String)>> {
    let mut tree: Value = dialect.parse_one(sql)?;
    let mut identities: Vec<Identity> = Vec::new();
    rewrite_tree(&mut tree, dialect, resolver, &mut identities, cache)?;
    validate_reference_identities(references, identities)?;
    if !references.is_empty() {
        dialect.generate(&tree, false)?;
    }
    let mut relations: Vec<(String, String)> = Vec::new();
    for reference in references {
        let Some(target) = resolver(&reference.name) else {
            return Err(AnalysisError(format!("Unresolved ref: {}", reference.name)));
        };
        if !relations.iter().any(|(cached, _)| *cached == target) {
            let canonical: String = cache.canonical(&target, dialect)?;
            relations.push((target, canonical));
        }
    }
    Ok(relations)
}

fn rewrite_tree(
    node: &mut Value,
    dialect: &SqlDialect,
    resolver: Resolver<'_>,
    identities: &mut Vec<Identity>,
    cache: &mut RelationCache,
) -> AnalysisResult<()> {
    match node {
        Value::Object(entries) => {
            if let Some(Value::Array(expressions)) = entries
                .get_mut("from")
                .and_then(Value::as_object_mut)
                .and_then(|from| from.get_mut("expressions"))
            {
                for expression in expressions.iter_mut() {
                    if let Some(replacement) =
                        replacement(expression, dialect, resolver, identities, cache)?
                    {
                        *expression = replacement;
                    }
                }
            }
            if let Some(Value::Array(joins)) = entries.get_mut("joins") {
                for join in joins.iter_mut() {
                    if let Some(join) = join.as_object_mut() {
                        let expression: Value = join.get("this").cloned().unwrap_or(Value::Null);
                        if let Some(replacement) =
                            replacement(&expression, dialect, resolver, identities, cache)?
                        {
                            join.insert("this".to_owned(), replacement);
                        }
                    }
                }
            }
            for value in entries.values_mut() {
                if matches!(value, Value::Object(_) | Value::Array(_)) {
                    rewrite_tree(value, dialect, resolver, identities, cache)?;
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                if matches!(item, Value::Object(_) | Value::Array(_)) {
                    rewrite_tree(item, dialect, resolver, identities, cache)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

/// A copy of the resolved relation for one relation expression, keeping any authored alias.
fn replacement(
    expression: &Value,
    dialect: &SqlDialect,
    resolver: Resolver<'_>,
    identities: &mut Vec<Identity>,
    cache: &mut RelationCache,
) -> AnalysisResult<Option<Value>> {
    let Some(entries) = expression.as_object() else {
        return Ok(None);
    };
    if let Some(alias) = object(entries.get("alias")) {
        let inner: Option<Value> = replacement(
            alias.get("this").unwrap_or(&Value::Null),
            dialect,
            resolver,
            identities,
            cache,
        )?;
        return Ok(inner.map(|inner| {
            let mut aliased: Object = alias.clone();
            aliased.insert("this".to_owned(), inner);
            let mut wrapper: Object = entries.clone();
            wrapper.insert("alias".to_owned(), Value::Object(aliased));
            Value::Object(wrapper)
        }));
    }
    let Some(target) = cache.reference_target(expression, dialect, resolver, identities)? else {
        return Ok(None);
    };
    Ok(Some(cache.relation(&target, dialect)?.tree.clone()))
}

fn reference_identity(expression: &Object) -> Option<Identity> {
    let function: &Object = object(expression.get("function"))?;
    let relation_type: &'static str = match text(function.get("name")) {
        Some(SOURCE_REFERENCE_FUNCTION) => "source",
        Some(MODEL_REFERENCE_FUNCTION) => "ref",
        _ => return None,
    };
    let arguments: &Vec<Value> = function.get("args")?.as_array()?;
    let name: String = scalar_name(arguments.first()?)?.to_owned();
    let ref_type: Option<String> =
        if arguments.len() == REFERENCE_WITH_TYPE_ARGUMENT_COUNT && relation_type == "ref" {
            ast_ref_type(&arguments[1])
        } else {
            None
        };
    Some((relation_type, name, ref_type))
}

fn ast_ref_type(argument: &Value) -> Option<String> {
    let equality: &Object = object(argument.as_object()?.get("eq"))?;
    let keyword: Option<&str> = equality.get("left").and_then(scalar_name);
    let value: &str = equality.get("right").and_then(scalar_name)?;
    (keyword == Some(REFERENCE_TYPE_KEYWORD) && REFERENCE_TYPES.contains(&value))
        .then(|| value.to_owned())
}

fn scalar_name(argument: &Value) -> Option<&str> {
    let argument: &Object = argument.as_object()?;
    if let Some(name) = object(argument.get("column")).and_then(|column| object(column.get("name")))
    {
        return text(name.get("name"));
    }
    text(object(argument.get("literal"))?.get("value"))
}

/// Parse one resolution target as a bare, unaliased relation tree.
///
/// # Errors
///
/// Returns the parse failure, or an error when the target is not exactly one unaliased relation.
pub(crate) fn parse_relation(target: &str, dialect: &SqlDialect) -> AnalysisResult<Value> {
    let invalid = || {
        AnalysisError(format!(
            "Resolved relation '{target}' is not a valid SQL relation"
        ))
    };
    let mut tree: Value = dialect.parse_one(&format!("SELECT * FROM {target}"))?;
    let select: &mut Object = tree
        .get_mut("select")
        .and_then(Value::as_object_mut)
        .ok_or_else(invalid)?;
    let single_relation: bool = object(select.get("from"))
        .and_then(|from| from.get("expressions"))
        .and_then(Value::as_array)
        .is_some_and(|expressions| expressions.len() == 1);
    let has_joins: bool = select
        .get("joins")
        .and_then(Value::as_array)
        .is_some_and(|joins| !joins.is_empty());
    if !single_relation || has_joins {
        return Err(invalid());
    }
    let relation: Value = select["from"]["expressions"][0].take();
    let entries: &Object = relation.as_object().ok_or_else(invalid)?;
    let payload_alias: bool = entries
        .values()
        .next()
        .and_then(Value::as_object)
        .is_some_and(|payload| payload.get("alias").is_some_and(|alias| !alias.is_null()));
    if entries.contains_key("alias") || payload_alias {
        return Err(AnalysisError(format!(
            "Resolved relation '{target}' must not define its own alias"
        )));
    }
    Ok(relation)
}

fn validate_reference_identities(
    references: &[ExpectedReference],
    mut actual: Vec<Identity>,
) -> AnalysisResult<()> {
    let mut expected: Vec<(String, String, Option<String>)> = references
        .iter()
        .map(|reference| {
            (
                reference.relation_type.clone(),
                reference.name.clone(),
                reference.ref_type.clone(),
            )
        })
        .collect();
    let mut actual: Vec<(String, String, Option<String>)> = actual
        .drain(..)
        .map(|(relation_type, name, ref_type)| (relation_type.to_owned(), name, ref_type))
        .collect();
    expected.sort();
    actual.sort();
    if expected != actual {
        return Err(AnalysisError::new(
            "__source(...) and __ref(...) calls are valid only in FROM or JOIN relation positions",
        ));
    }
    Ok(())
}

fn reject_reserved_database_placeholder(
    sql: &str,
    database_placeholder: &str,
) -> AnalysisResult<()> {
    if sql.contains(database_placeholder) {
        return Err(AnalysisError::new(
            "SQL contains the reserved adapter database placeholder",
        ));
    }
    Ok(())
}

/// Qualify physical table nodes with `database`, leaving CTE relations and qualified tables alone.
fn qualify_relations(node: &mut Value, database: &str, visible_ctes: &[String]) {
    match node {
        Value::Array(items) => {
            for item in items {
                qualify_relations(item, database, visible_ctes);
            }
        }
        Value::Object(entries) => {
            if let Some(table) = entries.get_mut("table").and_then(Value::as_object_mut)
                && table.contains_key("schema")
            {
                let unqualified: bool = identifier_name(table.get("name"))
                    .is_some_and(|name| !visible_ctes.iter().any(|cte| cte == name))
                    && table.get("schema").is_some_and(Value::is_null);
                if unqualified {
                    table.insert(
                        "schema".to_owned(),
                        json!({"name": database, "quoted": false, "trailing_comments": []}),
                    );
                }
                return;
            }
            for value in entries.values_mut() {
                match value {
                    Value::Object(child) => {
                        let child_ctes: Option<Vec<String>> = with_ctes(visible_ctes, child);
                        qualify_relations(
                            value,
                            database,
                            child_ctes.as_deref().unwrap_or(visible_ctes),
                        );
                    }
                    Value::Array(_) => qualify_relations(value, database, visible_ctes),
                    _ => {}
                }
            }
        }
        _ => {}
    }
}

/// Generate canonical and database-template forms of one non-model query.
///
/// # Errors
///
/// Returns parse or generation failures, or the reserved placeholder appearing in the query.
pub fn canonical_query_with_database_template(
    sql: &str,
    dialect: &SqlDialect,
    database_placeholder: &str,
) -> AnalysisResult<ResolvedQuery> {
    let tree: Value = dialect.parse_one(sql)?;
    let canonical_sql: String = dialect.generate(&tree, true)?;
    reject_reserved_database_placeholder(&canonical_sql, database_placeholder)?;
    let mut template_tree: Value = tree;
    qualify_relations(&mut template_tree, database_placeholder, &[]);
    Ok(ResolvedQuery {
        canonical_sql,
        database_template: dialect.generate(&template_tree, true)?,
    })
}
