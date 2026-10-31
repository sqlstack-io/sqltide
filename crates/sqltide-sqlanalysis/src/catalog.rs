//! Canonical query and ClickHouse catalog facts from one statement, and the small parse-and-check
//! helpers the compiler and executor use around generated SQL.

use std::collections::HashMap;

use serde_json::{Value, json};

use crate::tree::{
    AnalysisError, AnalysisResult, Object, SqlDialect, identifier_name, object, truthy,
};

const QUERY_KEYS: [&str; 4] = ["select", "union", "intersect", "except"];

/// Query clauses that make a SELECT more than a direct `SELECT * FROM relation` binding.
const DIRECT_BINDING_TRANSFORM_KEYS: [&str; 19] = [
    "with",
    "where_clause",
    "group_by",
    "having",
    "qualify",
    "order_by",
    "distribute_by",
    "cluster_by",
    "sort_by",
    "limit",
    "offset",
    "fetch",
    "distinct_on",
    "top",
    "sample",
    "prewhere",
    "windows",
    "connect",
    "into",
];

/// One physical relation, optionally qualified by its database.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RelationIdentity {
    pub database: Option<String>,
    pub name: String,
}

/// Canonical query and ClickHouse catalog facts from one SQL statement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogAnalysis {
    pub canonical_sql: String,
    pub query_sql: Option<String>,
    pub source_relations: Vec<RelationIdentity>,
    pub direct_source: Option<RelationIdentity>,
    pub target_relation: Option<RelationIdentity>,
    pub engine: Option<String>,
    pub ttl: Option<String>,
    pub settings: Vec<(String, String)>,
}

/// Analyze one query, CREATE TABLE or CREATE VIEW statement.
///
/// # Errors
///
/// Returns parse or generation failures, unsupported statements, and relations without names.
pub fn analyze_catalog(sql: &str, dialect: &SqlDialect) -> AnalysisResult<CatalogAnalysis> {
    let tree: Value = dialect.parse_one(sql)?;
    let canonical_sql: String = dialect.generate(&tree, false)?;
    if let Some(create_table) = object(tree.get("create_table")) {
        let (ttl, settings) = table_properties(create_table, dialect)?;
        return Ok(CatalogAnalysis {
            canonical_sql,
            query_sql: None,
            source_relations: Vec::new(),
            direct_source: None,
            target_relation: None,
            engine: table_engine(create_table, dialect)?,
            ttl,
            settings,
        });
    }
    if let Some(create_view) = object(tree.get("create_view")) {
        let Some(query) = create_view.get("query").filter(|query| query.is_object()) else {
            return Err(AnalysisError::new(
                "CREATE VIEW statement lacks an AS query",
            ));
        };
        let query_sql: String = dialect.generate(query, false)?;
        let target_relation: Option<RelationIdentity> = object(create_view.get("to_table"))
            .map(table_identity)
            .transpose()?;
        return Ok(CatalogAnalysis {
            canonical_sql,
            query_sql: Some(query_sql),
            source_relations: catalog_relations(query, &HashMap::new(), &[])?,
            direct_source: direct_relation(query)?,
            target_relation,
            engine: None,
            ttl: None,
            settings: Vec::new(),
        });
    }
    if QUERY_KEYS.iter().any(|key| tree.get(*key).is_some()) {
        return Ok(CatalogAnalysis {
            query_sql: Some(canonical_sql.clone()),
            canonical_sql,
            source_relations: catalog_relations(&tree, &HashMap::new(), &[])?,
            direct_source: direct_relation(&tree)?,
            target_relation: None,
            engine: None,
            ttl: None,
            settings: Vec::new(),
        });
    }
    Err(AnalysisError::new(
        "Catalog SQL must be a query, CREATE TABLE, or CREATE VIEW statement",
    ))
}

fn properties(create_table: &Object) -> impl Iterator<Item = &Object> {
    create_table
        .get("properties")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
}

type TableProperties = (Option<String>, Vec<(String, String)>);

fn table_properties(
    create_table: &Object,
    dialect: &SqlDialect,
) -> AnalysisResult<TableProperties> {
    let mut ttl: Option<String> = None;
    let mut settings: Vec<(String, String)> = Vec::new();
    for property in properties(create_table) {
        if let Some(ttl_payload) = object(property.get("merge_tree_t_t_l")) {
            ttl = match ttl_payload.get("expressions").and_then(Value::as_array) {
                Some(expressions) if !expressions.is_empty() => Some(
                    expressions
                        .iter()
                        .map(|expression| dialect.generate(expression, false))
                        .collect::<AnalysisResult<Vec<String>>>()?
                        .join(", "),
                ),
                _ => None,
            };
        }
        if let Some(settings_payload) = object(property.get("settings_property")) {
            settings = Vec::new();
            for expression in settings_payload
                .get("expressions")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(equality) = object(expression.get("eq")) {
                    let side = |key: &str| equality.get(key).unwrap_or(&Value::Null);
                    settings.push((
                        dialect.generate(side("left"), false)?,
                        dialect.generate(side("right"), false)?,
                    ));
                }
            }
        }
    }
    Ok((ttl, settings))
}

fn table_engine(create_table: &Object, dialect: &SqlDialect) -> AnalysisResult<Option<String>> {
    for property in properties(create_table) {
        if let Some(engine) = object(property.get("engine_property"))
            .and_then(|engine| engine.get("this"))
            .filter(|engine| engine.is_object())
        {
            return dialect.generate(engine, false).map(Some);
        }
    }
    Ok(None)
}

fn table_identity(table: &Object) -> AnalysisResult<RelationIdentity> {
    let Some(name) = identifier_name(table.get("name")) else {
        return Err(AnalysisError::new("Catalog relation lacks a table name"));
    };
    Ok(RelationIdentity {
        database: identifier_name(table.get("schema")).map(str::to_owned),
        name: name.to_owned(),
    })
}

fn direct_relation(tree: &Value) -> AnalysisResult<Option<RelationIdentity>> {
    let Some(select) = object(tree.get("select")).filter(|select| is_direct_binding_select(select))
    else {
        return Ok(None);
    };
    let Some(table) = object(select.get("from"))
        .and_then(|from| from.get("expressions"))
        .and_then(Value::as_array)
        .filter(|expressions| expressions.len() == 1)
        .and_then(|expressions| object(expressions[0].get("table")))
    else {
        return Ok(None);
    };
    let modified: bool = table.get("final_") == Some(&Value::Bool(true))
        || table.get("only") == Some(&Value::Bool(true))
        || table
            .get("table_sample")
            .is_some_and(|sample| !sample.is_null());
    if modified {
        return Ok(None);
    }
    table_identity(table).map(Some)
}

fn is_direct_binding_select(select: &Object) -> bool {
    let Some(star) = select
        .get("expressions")
        .and_then(Value::as_array)
        .filter(|expressions| expressions.len() == 1)
        .and_then(|expressions| object(expressions[0].get("star")))
    else {
        return false;
    };
    let qualified_star: bool = ["table", "except", "replace", "rename"]
        .iter()
        .any(|key| star.get(*key).is_some_and(|value| !value.is_null()));
    let has_joins: bool = select
        .get("joins")
        .and_then(Value::as_array)
        .is_some_and(|joins| !joins.is_empty());
    !qualified_star
        && !has_joins
        && select.get("distinct") != Some(&Value::Bool(true))
        && !DIRECT_BINDING_TRANSFORM_KEYS
            .iter()
            .any(|key| truthy(select.get(*key)))
}

type CteBodies<'a> = HashMap<String, &'a Value>;

fn cte_bodies<'a>(visible: &CteBodies<'a>, node: &'a Object) -> CteBodies<'a> {
    let mut bodies: CteBodies<'a> = visible.clone();
    for cte in object(node.get("with"))
        .and_then(|with| with.get("ctes"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
    {
        if let (Some(name), Some(body)) = (
            identifier_name(cte.get("alias")),
            cte.get("this").filter(|body| body.is_object()),
        ) {
            bodies.insert(name.to_owned(), body);
        }
    }
    bodies
}

fn push_unique(relations: &mut Vec<RelationIdentity>, found: Vec<RelationIdentity>) {
    for relation in found {
        if !relations.contains(&relation) {
            relations.push(relation);
        }
    }
}

/// Physical relations a node reads, following referenced CTE bodies, in first-seen order.
fn catalog_relations(
    node: &Value,
    visible: &CteBodies<'_>,
    resolving: &[String],
) -> AnalysisResult<Vec<RelationIdentity>> {
    let mut relations: Vec<RelationIdentity> = Vec::new();
    match node {
        Value::Array(items) => {
            for item in items {
                push_unique(&mut relations, catalog_relations(item, visible, resolving)?);
            }
        }
        Value::Object(entries) => {
            if let Some(select) = object(entries.get("select")) {
                return select_relations(select, visible, resolving);
            }
            let scoped: CteBodies<'_> = cte_bodies(visible, entries);
            for (key, value) in entries {
                if key != "with" {
                    push_unique(
                        &mut relations,
                        catalog_relations(value, &scoped, resolving)?,
                    );
                }
            }
        }
        _ => {}
    }
    Ok(relations)
}

fn without_keys(entries: &Object, keys: &[&str]) -> Value {
    Value::Object(
        entries
            .iter()
            .filter(|(key, _)| !keys.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
    )
}

fn select_relations(
    select: &Object,
    visible: &CteBodies<'_>,
    resolving: &[String],
) -> AnalysisResult<Vec<RelationIdentity>> {
    let scoped: CteBodies<'_> = cte_bodies(visible, select);
    let mut relations: Vec<RelationIdentity> = Vec::new();
    if let Some(expressions) = object(select.get("from"))
        .and_then(|from| from.get("expressions"))
        .and_then(Value::as_array)
    {
        push_unique(
            &mut relations,
            relation_slot_relations(expressions, &scoped, resolving)?,
        );
    }
    for join in select
        .get("joins")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let target: Value = join.get("this").cloned().unwrap_or(Value::Null);
        push_unique(
            &mut relations,
            relation_slot_relations(std::slice::from_ref(&target), &scoped, resolving)?,
        );
        if let Some(join) = join.as_object() {
            push_unique(
                &mut relations,
                catalog_relations(&without_keys(join, &["this"]), &scoped, resolving)?,
            );
        }
    }
    push_unique(
        &mut relations,
        catalog_relations(
            &without_keys(select, &["with", "from", "joins"]),
            &scoped,
            resolving,
        )?,
    );
    Ok(relations)
}

fn relation_slot_relations(
    nodes: &[Value],
    visible: &CteBodies<'_>,
    resolving: &[String],
) -> AnalysisResult<Vec<RelationIdentity>> {
    let mut relations: Vec<RelationIdentity> = Vec::new();
    for node in nodes {
        let Some(entries) = node.as_object() else {
            continue;
        };
        let Some(table) = object(entries.get("table")) else {
            push_unique(&mut relations, catalog_relations(node, visible, resolving)?);
            continue;
        };
        let identity: RelationIdentity = table_identity(table)?;
        let body: Option<&Value> = if identity.database.is_none() {
            visible.get(&identity.name).copied()
        } else {
            None
        };
        match body {
            None => push_unique(&mut relations, vec![identity]),
            Some(body) if !resolving.contains(&identity.name) => {
                let mut nested: Vec<String> = resolving.to_vec();
                nested.push(identity.name);
                push_unique(&mut relations, catalog_relations(body, visible, &nested)?);
            }
            Some(_) => {}
        }
    }
    Ok(relations)
}

/// Canonical top-level expressions of an authored expression list, without textual comma
/// splitting.
///
/// # Errors
///
/// Returns parse or generation failures, or a list that does not parse as a projection.
pub fn expression_list(sql: &str, dialect: &SqlDialect) -> AnalysisResult<Vec<String>> {
    let tree: Value = dialect.parse_one(&format!("SELECT {sql}"))?;
    let Some(select) = object(tree.get("select")) else {
        return Err(AnalysisError::new(
            "Expression list must parse as a SELECT projection",
        ));
    };
    let Some(expressions) = select.get("expressions").and_then(Value::as_array) else {
        return Err(AnalysisError::new(
            "Expression list has an invalid projection shape",
        ));
    };
    let generate_all = |items: &[Value]| -> AnalysisResult<Vec<String>> {
        items
            .iter()
            .map(|item| dialect.generate(item, false))
            .collect()
    };
    let [expression] = expressions.as_slice() else {
        return generate_all(expressions);
    };
    let Some(entries) = expression.as_object() else {
        return Err(AnalysisError::new(
            "Expression list has an invalid projection expression",
        ));
    };
    if let Some(items) = object(entries.get("tuple"))
        .and_then(|tuple| tuple.get("expressions"))
        .and_then(Value::as_array)
    {
        return generate_all(items);
    }
    if let Some(inner) = object(entries.get("paren"))
        .and_then(|paren| paren.get("this"))
        .filter(|inner| inner.is_object())
    {
        return Ok(vec![dialect.generate(inner, false)?]);
    }
    Ok(vec![dialect.generate(expression, false)?])
}

/// Require exactly one SELECT or set-operation query.
///
/// # Errors
///
/// Returns the parse failure or any other statement shape.
pub fn validate_query(sql: &str, dialect: &SqlDialect) -> AnalysisResult<()> {
    let tree: Value = dialect.parse_one(sql)?;
    if QUERY_KEYS.iter().any(|key| tree.get(*key).is_some()) {
        return Ok(());
    }
    Err(AnalysisError::new(
        "SQL statement must be a SELECT or set-operation query",
    ))
}

/// Require exactly one INSERT with a query body.
///
/// # Errors
///
/// Returns the parse failure or any other statement shape.
pub fn validate_insert_query(sql: &str, dialect: &SqlDialect) -> AnalysisResult<()> {
    let tree: Value = dialect.parse_one(sql)?;
    if object(tree.get("insert")).is_some_and(|insert| object(insert.get("query")).is_some()) {
        return Ok(());
    }
    Err(AnalysisError::new(
        "Replay output expects an INSERT SELECT query",
    ))
}

/// Parse and generate one statement in canonical form.
///
/// # Errors
///
/// Returns parse or generation failures.
pub fn canonicalize_sql(sql: &str, dialect: &SqlDialect) -> AnalysisResult<String> {
    dialect.generate(&dialect.parse_one(sql)?, false)
}

/// One dialect-safe SQL string literal.
///
/// # Errors
///
/// Returns the generation failure.
pub fn render_string_literal(value: &str, dialect: &SqlDialect) -> AnalysisResult<String> {
    dialect.generate(
        &json!({"literal": {"literal_type": "string", "value": value}}),
        false,
    )
}
