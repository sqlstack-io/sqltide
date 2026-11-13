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
                // Keep a WHERE, GROUP BY, or SET clause, so a changed clause is not hidden.
                Some(_) if has_ttl_clauses(ttl_payload) => {
                    let clause: String =
                        dialect.generate(&Value::Object(property.clone()), false)?;
                    Some(clause.strip_prefix("TTL ").unwrap_or(&clause).to_owned())
                }
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

/// One MergeTree table setting's value in the canonical form the catalog reads back.
///
/// # Errors
///
/// Returns parse or generation failures, and refuses text that is not exactly one setting.
pub fn canonicalize_setting(
    name: &str,
    value: &str,
    dialect: &SqlDialect,
) -> AnalysisResult<String> {
    let analysis: CatalogAnalysis = analyze_catalog(
        &format!(
            "CREATE TABLE t (x Int8) ENGINE = MergeTree ORDER BY tuple() SETTINGS {name} = {value}"
        ),
        dialect,
    )?;
    match analysis.settings.as_slice() {
        [(parsed, canonical)] if parsed == name => Ok(canonical.clone()),
        _ => Err(AnalysisError::new(
            "A setting value must be exactly one setting",
        )),
    }
}

/// Whether a TTL has a WHERE, GROUP BY, or SET clause, which Polyglot keeps for the whole TTL
/// rather than for its element, cutting off any elements after it.
fn has_ttl_clauses(ttl: &Object) -> bool {
    ["where_", "group", "aggregates"]
        .iter()
        .any(|key| ttl.get(*key).is_some_and(|clause| !clause.is_null()))
}

/// One MergeTree TTL in the canonical form ClickHouse stores it: each simple `INTERVAL n UNIT`
/// becomes a `toInterval<Unit>(n)` call with a numeric `n`, and a bare `DELETE` action, the
/// default, is dropped.
///
/// # Errors
///
/// Returns parse or generation failures, and refuses a TTL with a WHERE, GROUP BY, or SET clause,
/// which Polyglot cannot represent faithfully.
pub fn canonicalize_ttl(ttl: &str, dialect: &SqlDialect) -> AnalysisResult<String> {
    let mut tree: Value = dialect.parse_one(&format!(
        "CREATE TABLE t (x Int8) ENGINE = MergeTree ORDER BY tuple() TTL {ttl}"
    ))?;
    let clauses: bool = object(tree.get("create_table"))
        .into_iter()
        .flat_map(properties)
        .filter_map(|property| object(property.get("merge_tree_t_t_l")))
        .any(has_ttl_clauses);
    if clauses {
        return Err(AnalysisError::new(
            "A TTL with a WHERE, GROUP BY, or SET clause has no canonical form",
        ));
    }
    canonical_ttl_nodes(&mut tree);
    dialect.generate(&tree, false)
}

fn canonical_ttl_nodes(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            if let Some(replacement) = canonical_ttl_node(fields) {
                *value = replacement;
                canonical_ttl_nodes(value);
                return;
            }
            fields.values_mut().for_each(canonical_ttl_nodes);
        }
        Value::Array(items) => items.iter_mut().for_each(canonical_ttl_nodes),
        _ => {}
    }
}

/// The canonical replacement for one node, or `None` when it is already canonical.
fn canonical_ttl_node(fields: &Object) -> Option<Value> {
    if let Some(interval) = object(fields.get("interval")) {
        let unit: &Object = object(interval.get("unit"))?;
        if unit.get("type").and_then(Value::as_str) != Some("simple") {
            return None;
        }
        let unit: &str = unit.get("unit").and_then(Value::as_str)?;
        return Some(json!({"function": {
            "name": format!("toInterval{unit}"),
            "args": [interval.get("this")?],
            "distinct": false,
            "trailing_comments": [],
            "use_bracket_syntax": false,
            "no_parens": false,
            "quoted": false,
        }}));
    }
    if let Some(function) = object(fields.get("function")) {
        let name: &str = function.get("name").and_then(Value::as_str)?;
        let [argument] = function.get("args").and_then(Value::as_array)?.as_slice() else {
            return None;
        };
        let literal: &Object = object(argument.get("literal"))?;
        let numeric: bool = literal.get("literal_type").and_then(Value::as_str) == Some("string")
            && literal
                .get("value")
                .and_then(Value::as_str)
                .is_some_and(|amount| amount.parse::<f64>().is_ok());
        if !name.starts_with("toInterval") || !numeric {
            return None;
        }
        let mut function: Object = function.clone();
        function["args"][0]["literal"]["literal_type"] = json!("number");
        return Some(json!({"function": function}));
    }
    let action: &Object = object(fields.get("merge_tree_t_t_l_action"))?;
    let bare_delete: bool = action.get("delete") == Some(&json!({"boolean": {"value": true}}))
        && ["recompress", "to_disk", "to_volume"]
            .iter()
            .all(|key| action.get(*key).is_none_or(Value::is_null));
    bare_delete.then(|| action.get("this").cloned()).flatten()
}

/// Unquoted words that ClickHouse reads as values rather than identifiers in a SETTINGS clause.
const SETTING_KEYWORDS: [&str; 4] = ["default", "inf", "infinity", "nan"];

/// One MergeTree table setting value as the canonical SQL to render: a bare word such as `hot`,
/// which ClickHouse rejects as a value, becomes the string literal `'hot'`; `default` becomes
/// `DEFAULT`.
///
/// # Errors
///
/// Refuses text that is not exactly one SQL value, such as `1 2`, `1, 2`, or `'x' AS y`.
pub fn table_setting_sql(value: &str, dialect: &SqlDialect) -> AnalysisResult<String> {
    let invalid = || AnalysisError::new(format!("{value:?} is not exactly one SQL value"));
    // The newlines end any line comment, and the parentheses make leftover tokens a parse error.
    let tree: Value = dialect
        .parse_one(&format!("SELECT (\n{value}\n)"))
        .map_err(|_| invalid())?;
    let select: &Object = object(tree.get("select")).ok_or_else(invalid)?;
    let clauses: bool = select.iter().any(|(key, clause)| {
        key != "expressions"
            && !matches!(clause, Value::Null | Value::Bool(false))
            && clause.as_array().is_none_or(|items| !items.is_empty())
    });
    let expression: &Value = match select.get("expressions").and_then(Value::as_array) {
        Some(expressions) if !clauses && expressions.len() == 1 => &expressions[0],
        _ => return Err(invalid()),
    };
    let inner: &Value = object(expression.get("paren"))
        .and_then(|paren| paren.get("this"))
        .filter(|inner| {
            object(inner.get("tuple")).is_none() && object(inner.get("alias")).is_none()
        })
        .ok_or_else(invalid)?;
    if let Some(column) = object(inner.get("column"))
        && column.get("table").is_none_or(Value::is_null)
        && let Some(name) = identifier_name(column.get("name"))
    {
        let keyword: String = name.to_ascii_lowercase();
        let quoted: bool = truthy(object(column.get("name")).and_then(|name| name.get("quoted")));
        if quoted || !SETTING_KEYWORDS.contains(&keyword.as_str()) {
            return render_string_literal(name, dialect);
        }
        // ClickHouse stores `DEFAULT` in upper case, so the planner then compares it as unchanged.
        if keyword == "default" {
            return Ok("DEFAULT".to_owned());
        }
    }
    dialect.generate(inner, false)
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
