//! Split one typed row-level model into the query that keeps convertible rows and the query that
//! records the rest, so both come from a single rewrite and cannot drift.

use std::collections::HashSet;

use serde_json::Value;

use crate::model::{ModelError, ParsedModel, analyze_model};
use crate::projections::{Projection, ProjectionTypeCache};
use crate::tree::{AnalysisError, AnalysisResult, Object, SqlDialect, identifier_name, object};

const VALUE_PREFIX: &str = "__sqt_value_";
const PARTITION_COLUMN: &str = "_replay_partition";
const OFFSET_COLUMN: &str = "_replay_offset";
const NULLABLE_WRAPPER: &str = "nullable(";
const LOW_CARDINALITY_WRAPPER: &str = "lowcardinality(";
/// Types whose real `CAST` and `CAST(... AS Nullable(T))` agree on which rows raise.
const NULLABLE_CAST_TYPE_PREFIXES: [&str; 10] = [
    "float32",
    "float64",
    "date32",
    "date",
    "datetime64",
    "datetime",
    "string",
    "uuid",
    "ipv4",
    "ipv6",
];
/// Types where a `Nullable` cast raises but `accurateCastOrNull` agrees with `CAST`.
const ACCURATE_TYPE_PREFIXES: [&str; 3] = ["bool", "decimal", "fixedstring"];
/// Type names `accurateCastOrNull` cannot return and a model cannot sensibly skip.
const UNCHECKABLE_TYPE_PREFIXES: [&str; 8] = [
    "nested(",
    "dynamic",
    "aggregatefunction(",
    "point",
    "ring",
    "polygon",
    "linestring",
    "multi",
];
/// SELECT keys holding clauses that stay in the inner query with the authored FROM.
const RELATION_KEYS: [&str; 3] = ["expressions", "from", "with"];

/// The generated query pair of one quarantine model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineSplit {
    pub good_query: String,
    pub dead_letter_query: String,
}

/// Why one output column makes a model ineligible for quarantine.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineIneligibility {
    pub column: String,
    pub reason: String,
}

struct OuterColumn {
    name: String,
    type_sql: String,
}

/// The first output column that prevents the split, in authored order.
///
/// # Errors
///
/// Returns a failure when the query cannot be parsed as a typed model.
pub fn quarantine_ineligibility(
    sql: &str,
    dialect: &SqlDialect,
) -> AnalysisResult<Option<QuarantineIneligibility>> {
    let model: ParsedModel = parsed(sql, dialect)?;
    let select: &Object = select_of(&model)?;
    ineligibility(select, &model.projections)
}

/// The kept-rows query and the dead-letter query for `model_name`.
///
/// # Errors
///
/// Returns a failure for an unparseable or ineligible query.
pub fn quarantine_split(
    sql: &str,
    model_name: &str,
    dialect: &SqlDialect,
) -> AnalysisResult<QuarantineSplit> {
    let model: ParsedModel = parsed(sql, dialect)?;
    let select: &Object = select_of(&model)?;
    if let Some(blocked) = ineligibility(select, &model.projections)? {
        return Err(AnalysisError(format!(
            "Column '{}' cannot be quarantined: {}",
            blocked.column, blocked.reason
        )));
    }
    let columns: Vec<OuterColumn> = model
        .projections
        .iter()
        .map(|projection| OuterColumn {
            name: projection.name.clone(),
            type_sql: projection.type_sql.clone(),
        })
        .collect();
    let inner: String = inner_query(&model, dialect)?;
    let checks: Vec<(String, String)> = columns
        .iter()
        .enumerate()
        .filter_map(|(index, column)| {
            invalid_check(index, &column.type_sql).map(|check| (column.name.clone(), check))
        })
        .collect();
    let any_invalid: Option<String> = (!checks.is_empty()).then(|| {
        checks
            .iter()
            .map(|(_, check)| check.as_str())
            .collect::<Vec<_>>()
            .join(" OR ")
    });
    let projected: String = columns
        .iter()
        .enumerate()
        .map(|(index, column)| {
            format!(
                "CAST({VALUE_PREFIX}{index} AS {}) AS {}",
                column.type_sql,
                identifier(&column.name)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    let good_filter: String = any_invalid
        .as_ref()
        .map_or_else(String::new, |invalid| format!(" WHERE NOT ({invalid})"));
    let dead_filter: String = any_invalid
        .as_ref()
        .map_or_else(|| "false".to_owned(), |invalid| format!("({invalid})"));
    Ok(QuarantineSplit {
        good_query: format!("SELECT {projected} FROM ({inner}){good_filter}"),
        dead_letter_query: format!(
            "SELECT {} FROM ({inner}) WHERE {dead_filter}",
            dead_letter_columns(model_name, &columns, &checks)
        ),
    })
}

fn parsed(sql: &str, dialect: &SqlDialect) -> AnalysisResult<ParsedModel> {
    let mut type_cache: ProjectionTypeCache = ProjectionTypeCache::new();
    analyze_model(sql, dialect, &mut type_cache).map_err(|error| match error {
        ModelError::Analysis(failure) => failure,
        other => AnalysisError(format!("Quarantine requires a typed model: {other:?}")),
    })
}

fn select_of(model: &ParsedModel) -> AnalysisResult<&Object> {
    object(model.tree.get("select"))
        .ok_or_else(|| AnalysisError::new("Quarantine expects a SELECT query"))
}

fn expressions(select: &Object) -> &[Value] {
    select
        .get("expressions")
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

/// The authored expression inside one `CAST(expr AS T) AS name` projection.
fn authored_expression(projection: &Value) -> Option<&Value> {
    let aliased: &Object = object(object(projection.get("alias"))?.get("this"))?;
    let cast: &Object = object(aliased.get("cast")).or_else(|| object(aliased.get("try_cast")))?;
    cast.get("this")
}

fn ineligibility(
    select: &Object,
    projections: &[Projection],
) -> AnalysisResult<Option<QuarantineIneligibility>> {
    let trees: &[Value] = expressions(select);
    let outputs: HashSet<&str> = projections.iter().map(|item| item.name.as_str()).collect();
    if let Some(blocked) = query_level_ineligibility(select) {
        return Ok(Some(blocked));
    }
    for (projection, tree) in projections.iter().zip(trees) {
        let expression: &Value = authored_expression(tree)
            .ok_or_else(|| AnalysisError::new("Quarantine expects CAST projections"))?;
        let mut names: Vec<String> = Vec::new();
        unqualified_columns(expression, &mut names);
        if let Some(sibling) = names
            .iter()
            .find(|name| outputs.contains(name.as_str()) && **name != projection.name)
        {
            return Ok(Some(QuarantineIneligibility {
                column: projection.name.clone(),
                reason: format!(
                    "its expression references the output column '{sibling}' by alias; \
                     reference the source column or compute it in a subquery"
                ),
            }));
        }
        if is_uncheckable(&projection.type_sql) {
            return Ok(Some(QuarantineIneligibility {
                column: projection.name.clone(),
                reason: format!(
                    "its type {} cannot be checked with accurateCastOrNull",
                    projection.type_sql
                ),
            }));
        }
    }
    let mut clause_names: Vec<String> = Vec::new();
    for (key, clause) in select {
        if !RELATION_KEYS.contains(&key.as_str()) {
            unqualified_columns(clause, &mut clause_names);
        }
    }
    Ok(clause_names
        .iter()
        .find(|name| outputs.contains(name.as_str()))
        .map(|alias| QuarantineIneligibility {
            column: alias.clone(),
            reason: "a clause outside the projection references this output by alias".to_owned(),
        }))
}

fn query_level_ineligibility(select: &Object) -> Option<QuarantineIneligibility> {
    let clause: &str = if select.get("distinct") == Some(&Value::Bool(true))
        || select
            .get("distinct_on")
            .is_some_and(|value| !value.is_null())
    {
        "DISTINCT"
    } else if select.get("limit_by").is_some_and(|value| !value.is_null()) {
        "LIMIT BY"
    } else {
        return None;
    };
    Some(QuarantineIneligibility {
        column: String::new(),
        reason: format!("its outer query uses {clause}"),
    })
}

/// Unqualified column names under `node`, not descending into nested queries.
fn unqualified_columns(node: &Value, names: &mut Vec<String>) {
    match node {
        Value::Array(items) => {
            for item in items {
                unqualified_columns(item, names);
            }
        }
        Value::Object(entries) => {
            if entries.contains_key("select") {
                return;
            }
            if let Some(column) = object(entries.get("column")) {
                if object(column.get("table")).is_none()
                    && let Some(name) = identifier_name(column.get("name"))
                {
                    names.push(name.to_owned());
                }
                return;
            }
            for value in entries.values() {
                unqualified_columns(value, names);
            }
        }
        _ => {}
    }
}

fn is_uncheckable(type_sql: &str) -> bool {
    let lowered: String = type_sql.to_lowercase();
    UNCHECKABLE_TYPE_PREFIXES
        .iter()
        .any(|prefix| lowered.starts_with(prefix))
}

/// The predicate marking row `index` invalid for `type_sql`, or `None` for an unchecked column.
fn invalid_check(index: usize, type_sql: &str) -> Option<String> {
    let lowered: String = type_sql.to_lowercase();
    if lowered.starts_with(NULLABLE_WRAPPER) {
        return None;
    }
    if lowered.starts_with(LOW_CARDINALITY_WRAPPER) {
        let inner: &str = type_sql[LOW_CARDINALITY_WRAPPER.len()..].strip_suffix(')')?;
        return invalid_check(index, inner);
    }
    let value: String = format!("{VALUE_PREFIX}{index}");
    if ACCURATE_TYPE_PREFIXES
        .iter()
        .any(|prefix| lowered.starts_with(prefix))
    {
        return Some(format!(
            "accurateCastOrNull({value}, {}) IS NULL",
            literal(type_sql)
        ));
    }
    if is_integer_type(&lowered) {
        let widest: &str = if lowered.starts_with("uint") {
            "UInt256"
        } else {
            "Int256"
        };
        return Some(format!(
            "(CAST({value} AS Nullable({type_sql})) IS NULL AND CAST({value} AS \
             Nullable({widest})) IS NULL)"
        ));
    }
    NULLABLE_CAST_TYPE_PREFIXES
        .iter()
        .any(|prefix| lowered.starts_with(prefix))
        .then(|| format!("CAST({value} AS Nullable({type_sql})) IS NULL"))
}

fn is_integer_type(lowered: &str) -> bool {
    let digits: &str = lowered
        .strip_prefix("uint")
        .or_else(|| lowered.strip_prefix("int"))
        .unwrap_or_default();
    !digits.is_empty() && digits.chars().all(|character| character.is_ascii_digit())
}

fn literal(text: &str) -> String {
    format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
}

fn identifier(name: &str) -> String {
    let plain: bool = name.chars().enumerate().all(|(position, character)| {
        character == '_'
            || character.is_ascii_alphabetic()
            || (position > 0 && character.is_ascii_digit())
    });
    if plain {
        name.to_owned()
    } else {
        format!("`{}`", name.replace('`', "``"))
    }
}

/// The authored query with each projection renamed to its private value, cast removed.
fn inner_query(model: &ParsedModel, dialect: &SqlDialect) -> AnalysisResult<String> {
    let mut tree: Value = model.tree.clone();
    let projections: &mut Vec<Value> = tree
        .get_mut("select")
        .and_then(|select| select.get_mut("expressions"))
        .and_then(Value::as_array_mut)
        .ok_or_else(|| AnalysisError::new("Quarantine expects SELECT expressions"))?;
    for (index, projection) in projections.iter_mut().enumerate() {
        let expression: Value = authored_expression(projection)
            .cloned()
            .ok_or_else(|| AnalysisError::new("Quarantine expects CAST projections"))?;
        let Some(alias) = projection.get_mut("alias").and_then(Value::as_object_mut) else {
            return Err(AnalysisError::new("Quarantine expects aliased projections"));
        };
        alias.insert("this".to_owned(), expression);
        let Some(name) = alias.get_mut("alias").and_then(Value::as_object_mut) else {
            return Err(AnalysisError::new("Quarantine expects aliased projections"));
        };
        name.insert(
            "name".to_owned(),
            Value::String(format!("{VALUE_PREFIX}{index}")),
        );
        name.insert("quoted".to_owned(), Value::Bool(false));
    }
    dialect.generate(&tree, false)
}

fn dead_letter_columns(
    model_name: &str,
    columns: &[OuterColumn],
    checks: &[(String, String)],
) -> String {
    let failed: String = if checks.is_empty() {
        "CAST([] AS Array(String))".to_owned()
    } else {
        format!(
            "arrayFilter(failed_name -> failed_name != '', [{}])",
            checks
                .iter()
                .map(|(name, check)| format!("if({check}, {}, '')", literal(name)))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    let payload: String = columns
        .iter()
        .enumerate()
        .map(|(index, column)| {
            format!(
                "{}, toNullable(toString({VALUE_PREFIX}{index}))",
                literal(&column.name)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "{} AS model_name, {failed} AS failed_columns, toJSONString(map({payload})) AS payload, \
         {} AS kafka_partition, {} AS kafka_offset, now64(3) AS quarantined_at",
        literal(model_name),
        lineage_value(columns, PARTITION_COLUMN, "Int32"),
        lineage_value(columns, OFFSET_COLUMN, "Int64"),
    )
}

fn lineage_value(columns: &[OuterColumn], name: &str, column_type: &str) -> String {
    columns
        .iter()
        .position(|column| column.name == name)
        .map_or_else(
            || format!("CAST(NULL AS Nullable({column_type}))"),
            |index| format!("accurateCastOrNull({VALUE_PREFIX}{index}, '{column_type}')"),
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn split(sql: &str) -> QuarantineSplit {
        quarantine_split(sql, "orders", &SqlDialect::new("clickhouse")).unwrap()
    }

    fn blocked(sql: &str) -> QuarantineIneligibility {
        quarantine_ineligibility(sql, &SqlDialect::new("clickhouse"))
            .unwrap()
            .unwrap()
    }

    #[test]
    fn splits_self_shadowed_columns_through_private_values() {
        let generated = split(
            "SELECT CAST(v AS String) AS v, n::Nullable(Int32) AS n, \
             CAST(_replay_offset AS Int64) AS _replay_offset FROM raw__orders WHERE id > 0",
        );
        assert_eq!(
            generated.good_query,
            "SELECT CAST(__sqt_value_0 AS String) AS v, CAST(__sqt_value_1 AS Nullable(Int32)) \
             AS n, CAST(__sqt_value_2 AS Int64) AS _replay_offset FROM (SELECT v AS __sqt_value_0, \
             n AS __sqt_value_1, _replay_offset AS __sqt_value_2 FROM raw__orders WHERE id > 0) \
             WHERE NOT (CAST(__sqt_value_0 AS Nullable(String)) IS NULL OR \
             (CAST(__sqt_value_2 AS Nullable(Int64)) IS NULL AND CAST(__sqt_value_2 AS \
             Nullable(Int256)) IS NULL))"
        );
        assert!(
            generated
                .dead_letter_query
                .contains("if(CAST(__sqt_value_0 AS Nullable(String)) IS NULL, 'v', '')")
        );
        assert!(
            generated
                .dead_letter_query
                .contains("accurateCastOrNull(__sqt_value_2, 'Int64') AS kafka_offset")
        );
        assert!(
            generated
                .dead_letter_query
                .contains("CAST(NULL AS Nullable(Int32)) AS kafka_partition")
        );
    }

    #[test]
    fn checks_low_cardinality_through_its_value_type() {
        let generated = split(
            "SELECT CAST(a AS LowCardinality(String)) AS a, \
             CAST(b AS LowCardinality(Nullable(String))) AS b FROM raw__orders",
        );
        assert!(
            generated
                .good_query
                .contains("CAST(__sqt_value_0 AS Nullable(String)) IS NULL")
        );
        assert!(
            !generated
                .good_query
                .contains("CAST(__sqt_value_1 AS Nullable(")
        );
    }

    #[test]
    fn rejects_sibling_aliases_and_unconvertible_types() {
        let sibling = blocked("SELECT CAST(x AS Int32) AS a, CAST(a + 1 AS Int32) AS b FROM t");
        assert_eq!(sibling.column, "b");
        assert!(sibling.reason.contains("'a'"));
        let unsupported = blocked("SELECT CAST(x AS Dynamic) AS a FROM t");
        assert_eq!(unsupported.column, "a");
        assert!(unsupported.reason.contains("Dynamic"));
        let clause = blocked("SELECT CAST(x AS Int32) AS a FROM t WHERE a > 1");
        assert_eq!(clause.column, "a");
        let self_shadow = blocked("SELECT CAST(v AS String) AS v FROM t WHERE v != ''");
        assert_eq!(self_shadow.column, "v");
        for sql in [
            "SELECT DISTINCT CAST(v AS String) AS v2 FROM t",
            "SELECT CAST(v AS String) AS v2 FROM t LIMIT 1 BY v",
        ] {
            assert!(blocked(sql).column.is_empty());
        }
    }

    #[test]
    fn leaves_container_columns_unchecked() {
        let generated = split(
            "SELECT CAST(tags AS Array(String)) AS tags, CAST(attrs AS Map(String, String)) AS \
             attrs, CAST(pair AS Tuple(Int32, String)) AS pair, CAST(v AS String) AS v FROM t",
        );
        assert!(
            generated
                .good_query
                .contains("WHERE NOT (CAST(__sqt_value_3 AS Nullable(")
        );
        assert!(
            !generated
                .good_query
                .contains("accurateCastOrNull(__sqt_value_0")
        );
        assert!(
            generated
                .good_query
                .contains("CAST(__sqt_value_0 AS Array(String)) AS tags")
        );
    }

    #[test]
    fn selects_the_check_by_type_family() {
        let generated = split(
            "SELECT CAST(a AS Decimal(10, 2)) AS a, CAST(b AS FixedString(2)) AS b, \
             CAST(c AS Bool) AS c, CAST(d AS Enum8('x' = 1)) AS d, CAST(e AS Date) AS e FROM t",
        );
        assert!(
            generated
                .good_query
                .contains("accurateCastOrNull(__sqt_value_0, 'Decimal(10, 2)')")
        );
        assert!(
            generated
                .good_query
                .contains("accurateCastOrNull(__sqt_value_1, 'FixedString(2)')")
        );
        assert!(
            generated
                .good_query
                .contains("accurateCastOrNull(__sqt_value_2, 'Bool')")
        );
        assert!(!generated.good_query.contains("__sqt_value_3 AS Nullable"));
        assert!(
            generated
                .good_query
                .to_lowercase()
                .contains("cast(__sqt_value_4 as nullable(date)) is null")
        );
    }

    #[test]
    fn allows_self_references_and_qualified_names() {
        let dialect = SqlDialect::new("clickhouse");
        for sql in [
            "SELECT CAST(v AS String) AS v FROM t",
            "SELECT CAST(t.a AS Int32) AS a, CAST(t.a + 1 AS Int32) AS b FROM t",
        ] {
            assert_eq!(quarantine_ineligibility(sql, &dialect).unwrap(), None);
        }
    }
}
