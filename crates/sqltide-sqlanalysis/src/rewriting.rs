//! Rewrite relations, conjoin a predicate and prepend CTEs in one parsed SELECT, as replay and
//! population queries do.

use serde_json::{Value, json};

use crate::facts::tree_aggregate_facts;
use crate::relations::parse_relation;
use crate::tree::{AnalysisError, AnalysisResult, Object, SqlDialect, identifier_name, object};

/// Fields that carry an authored relation's alias onto its replacement.
const RELATION_ALIAS_FIELDS: [&str; 5] = [
    "alias",
    "alias_explicit_as",
    "alias_keyword",
    "column_aliases",
    "trailing_comments",
];

/// One eligible relation identity and the relation SQL that replaces it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelationRewrite {
    pub source_name: String,
    pub target_relation: String,
    /// The databases the source may be qualified with, where `None` stands for unqualified.
    /// `None` overall matches any qualification.
    pub source_databases: Option<Vec<Option<String>>>,
    pub preserve_source_database: bool,
}

/// One named query to prepend as a CTE.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NamedQuery {
    pub name: String,
    pub query: String,
}

/// A rewritten query with the aggregate facts of its final tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RewrittenQuery {
    pub query: String,
    pub function_names: Vec<String>,
    pub has_group_by: bool,
}

fn select_mut(tree: &mut Value) -> AnalysisResult<&mut Object> {
    tree.get_mut("select")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| AnalysisError::new("Query rewrite expects a SELECT query"))
}

/// Rewrite relations, then conjoin `predicate`, then prepend `ctes`, and generate the result.
///
/// # Errors
///
/// Returns parse failures, a non-SELECT query, invalid predicates or CTEs, and CTE name
/// collisions, in that order of checking.
pub fn rewrite_query(
    sql: &str,
    dialect: &SqlDialect,
    rewrites: &[RelationRewrite],
    predicate: Option<&str>,
    ctes: &[NamedQuery],
) -> AnalysisResult<RewrittenQuery> {
    let mut tree: Value = dialect.parse_one(sql)?;
    rewrite_select(select_mut(&mut tree)?, rewrites, &[], dialect)?;
    if let Some(predicate) = predicate {
        append_predicate(select_mut(&mut tree)?, predicate, dialect)?;
    }
    prepend_ctes(&mut tree, ctes, dialect)?;
    let (function_names, has_group_by) = tree_aggregate_facts(&tree);
    Ok(RewrittenQuery {
        query: dialect.generate(&tree, false)?,
        function_names,
        has_group_by,
    })
}

/// The aggregate facts of one SELECT, for queries rewritten textually.
///
/// # Errors
///
/// Returns the parse failure or a non-SELECT query.
pub fn select_aggregate_facts(
    sql: &str,
    dialect: &SqlDialect,
) -> AnalysisResult<(Vec<String>, bool)> {
    let mut tree: Value = dialect.parse_one(sql)?;
    select_mut(&mut tree)?;
    Ok(tree_aggregate_facts(&tree))
}

fn select_ctes(select: &Object) -> &[Value] {
    object(select.get("with"))
        .and_then(|with| with.get("ctes"))
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

fn cte_names(ctes: &[Value]) -> Vec<String> {
    ctes.iter()
        .filter_map(Value::as_object)
        .filter_map(|cte| identifier_name(cte.get("alias")))
        .map(str::to_owned)
        .collect()
}

fn rewrite_select(
    select: &mut Object,
    rewrites: &[RelationRewrite],
    visible_ctes: &[String],
    dialect: &SqlDialect,
) -> AnalysisResult<()> {
    let local_names: Vec<String> = cte_names(select_ctes(select));
    let mut all_visible: Vec<String> = visible_ctes.to_vec();
    all_visible.extend(local_names);
    let recursive: bool = object(select.get("with"))
        .is_some_and(|with| with.get("recursive") == Some(&Value::Bool(true)));
    let mut cte_visible: Vec<String> = if recursive {
        all_visible.clone()
    } else {
        visible_ctes.to_vec()
    };
    if let Some(Value::Array(ctes)) = select
        .get_mut("with")
        .and_then(Value::as_object_mut)
        .and_then(|with| with.get_mut("ctes"))
    {
        for cte in ctes.iter_mut() {
            if let Some(entries) = cte.as_object_mut() {
                if let Some(body) = entries.get_mut("this") {
                    rewrite_node(body, rewrites, &cte_visible, dialect)?;
                }
                if let Some(name) = identifier_name(entries.get("alias")) {
                    cte_visible.push(name.to_owned());
                }
            }
        }
    }
    rewrite_relation_slots(select, rewrites, &all_visible, dialect)?;
    for (key, value) in select.iter_mut() {
        if !matches!(key.as_str(), "from" | "joins" | "with") {
            rewrite_node(value, rewrites, &all_visible, dialect)?;
        }
    }
    Ok(())
}

fn rewrite_node(
    node: &mut Value,
    rewrites: &[RelationRewrite],
    visible_ctes: &[String],
    dialect: &SqlDialect,
) -> AnalysisResult<()> {
    match node {
        Value::Array(items) => {
            for item in items {
                rewrite_node(item, rewrites, visible_ctes, dialect)?;
            }
        }
        Value::Object(entries) => {
            if let Some(select) = entries.get_mut("select").and_then(Value::as_object_mut) {
                return rewrite_select(select, rewrites, visible_ctes, dialect);
            }
            for value in entries.values_mut() {
                rewrite_node(value, rewrites, visible_ctes, dialect)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn rewrite_relation_slots(
    select: &mut Object,
    rewrites: &[RelationRewrite],
    visible_ctes: &[String],
    dialect: &SqlDialect,
) -> AnalysisResult<()> {
    if let Some(Value::Array(expressions)) = select
        .get_mut("from")
        .and_then(Value::as_object_mut)
        .and_then(|from| from.get_mut("expressions"))
    {
        for relation in expressions.iter_mut() {
            rewrite_relation(relation, rewrites, visible_ctes, dialect)?;
        }
    }
    if let Some(Value::Array(joins)) = select.get_mut("joins") {
        for join in joins.iter_mut() {
            if let Some(join) = join.as_object_mut() {
                let relation: &mut Value = join.entry("this").or_insert(Value::Null);
                rewrite_relation(relation, rewrites, visible_ctes, dialect)?;
            }
        }
    }
    Ok(())
}

/// Replace one relation in place when a rewrite matches it, otherwise rewrite inside it.
fn rewrite_relation(
    relation: &mut Value,
    rewrites: &[RelationRewrite],
    visible_ctes: &[String],
    dialect: &SqlDialect,
) -> AnalysisResult<()> {
    let Some(entries) = relation.as_object() else {
        return Ok(());
    };
    let Some(table) = object(entries.get("table")) else {
        return rewrite_node(relation, rewrites, visible_ctes, dialect);
    };
    let Some(table_name) = identifier_name(table.get("name")) else {
        return Ok(());
    };
    let database: Option<&str> = identifier_name(table.get("schema"));
    if database.is_none() && visible_ctes.iter().any(|cte| cte == table_name) {
        return Ok(());
    }
    let Some(rewrite) = matching_rewrite(rewrites, table_name, database) else {
        return Ok(());
    };
    let mut replacement: Value = parse_relation(&rewrite.target_relation, dialect)?;
    if rewrite.preserve_source_database {
        let Some(replacement_table) = replacement.get_mut("table").and_then(Value::as_object_mut)
        else {
            return Err(AnalysisError::new(
                "Database preservation requires a table replacement",
            ));
        };
        replacement_table.insert(
            "schema".to_owned(),
            table.get("schema").cloned().unwrap_or(Value::Null),
        );
    }
    let Some(payload) = replacement
        .as_object_mut()
        .and_then(|entries| entries.values_mut().next())
        .and_then(Value::as_object_mut)
    else {
        return Err(AnalysisError::new(
            "Replacement relation has an invalid shape",
        ));
    };
    for field in RELATION_ALIAS_FIELDS {
        if let Some(value) = table.get(field) {
            payload.insert(field.to_owned(), value.clone());
        }
    }
    *relation = replacement;
    Ok(())
}

fn matching_rewrite<'a>(
    rewrites: &'a [RelationRewrite],
    table_name: &str,
    database: Option<&str>,
) -> Option<&'a RelationRewrite> {
    rewrites.iter().find(|rewrite| {
        rewrite.source_name == table_name
            && rewrite.source_databases.as_ref().is_none_or(|databases| {
                databases
                    .iter()
                    .any(|candidate| candidate.as_deref() == database)
            })
    })
}

fn append_predicate(
    select: &mut Object,
    predicate: &str,
    dialect: &SqlDialect,
) -> AnalysisResult<()> {
    let invalid = || AnalysisError::new("Replay boundary is not a valid SQL predicate");
    let mut predicate_tree: Value = dialect.parse_one(&format!("SELECT 1 WHERE {predicate}"))?;
    let replay_predicate: Value = object(select_mut(&mut predicate_tree)?.get("where_clause"))
        .ok_or_else(invalid)?
        .get("this")
        .filter(|value| value.is_object())
        .ok_or_else(invalid)?
        .clone();
    let Some(existing_where) = select
        .get_mut("where_clause")
        .and_then(Value::as_object_mut)
    else {
        select.insert("where_clause".to_owned(), json!({"this": replay_predicate}));
        return Ok(());
    };
    let Some(existing_predicate) = existing_where.get("this").filter(|value| value.is_object())
    else {
        return Err(AnalysisError::new(
            "Existing WHERE clause is not a valid SQL predicate",
        ));
    };
    let conjunction: Value = json!({"and": {
        "left": parenthesized(existing_predicate.clone()),
        "left_comments": [],
        "operator_comments": [],
        "right": parenthesized(replay_predicate),
        "trailing_comments": [],
    }});
    existing_where.insert("this".to_owned(), conjunction);
    Ok(())
}

fn parenthesized(predicate: Value) -> Value {
    json!({"paren": {"this": predicate, "trailing_comments": []}})
}

fn prepend_ctes(tree: &mut Value, ctes: &[NamedQuery], dialect: &SqlDialect) -> AnalysisResult<()> {
    if ctes.is_empty() {
        return Ok(());
    }
    let select: &mut Object = select_mut(tree)?;
    let existing_names: Vec<String> = cte_names(select_ctes(select));
    let mut requested: Vec<&str> = Vec::with_capacity(ctes.len());
    for cte in ctes {
        if existing_names.contains(&cte.name) || requested.contains(&&*cte.name) {
            return Err(AnalysisError::new(
                "Replay CTE name collides with an authored CTE",
            ));
        }
        requested.push(&cte.name);
    }
    let new_ctes: Vec<Value> = ctes
        .iter()
        .map(|cte| parse_cte(cte, dialect))
        .collect::<AnalysisResult<_>>()?;
    if let Some(with) = select.get_mut("with").and_then(Value::as_object_mut) {
        let existing: Vec<Value> = match with.get_mut("ctes") {
            Some(Value::Array(existing)) => std::mem::take(existing),
            _ => Vec::new(),
        };
        let mut combined: Vec<Value> = new_ctes;
        combined.extend(existing);
        with.insert("ctes".to_owned(), Value::Array(combined));
        return Ok(());
    }
    let mut template: Value = dialect.parse_one(&format!(
        "WITH {} AS ({}) SELECT 1",
        ctes[0].name, ctes[0].query
    ))?;
    let Some(mut with) = select_mut(&mut template)?
        .remove("with")
        .filter(Value::is_object)
    else {
        return Err(AnalysisError::new("Replay CTE could not be parsed"));
    };
    with["ctes"] = Value::Array(new_ctes);
    select.insert("with".to_owned(), with);
    Ok(())
}

fn parse_cte(cte: &NamedQuery, dialect: &SqlDialect) -> AnalysisResult<Value> {
    let mut tree: Value =
        dialect.parse_one(&format!("WITH {} AS ({}) SELECT 1", cte.name, cte.query))?;
    let select: &mut Object = select_mut(&mut tree)?;
    let parsed: Vec<Value> = select_ctes(select).to_vec();
    let [parsed] = <[Value; 1]>::try_from(parsed)
        .map_err(|_| AnalysisError(format!("Replay CTE '{}' could not be parsed", cte.name)))?;
    let Some(entries) = parsed.as_object() else {
        return Err(AnalysisError(format!(
            "Replay CTE '{}' could not be parsed",
            cte.name
        )));
    };
    if identifier_name(entries.get("alias")) != Some(cte.name.as_str()) {
        return Err(AnalysisError(format!(
            "Replay CTE '{}' has an invalid name",
            cte.name
        )));
    }
    Ok(parsed)
}
