//! Collect aggregate, relation-slot and raw-relation facts in one traversal of a model tree.

use serde_json::Value;

use crate::aggregates::aggregate_key_name;
use crate::tree::{identifier_name, is_nested, object, with_ctes};

/// One step from a tree root to a child.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Step {
    Key(String),
    Index(usize),
}

pub(crate) type Path = Vec<Step>;

pub(crate) fn at<'a>(tree: &'a Value, path: &[Step]) -> Option<&'a Value> {
    path.iter().try_fold(tree, |node, step| match step {
        Step::Key(key) => node.get(key.as_str()),
        Step::Index(index) => node.get(*index),
    })
}

#[derive(Debug, Default)]
pub(crate) struct TreeFacts {
    pub(crate) function_names: Vec<String>,
    pub(crate) has_group_by: bool,
    /// FROM and JOIN relation positions, in the order resolution validates them.
    pub(crate) reference_slots: Vec<Path>,
    /// The first physical relation that is neither a CTE nor a resolved reference.
    pub(crate) raw_relation: Option<String>,
}

pub(crate) fn collect_tree_facts(tree: &Value) -> TreeFacts {
    let mut facts: TreeFacts = TreeFacts::default();
    let mut path: Path = Vec::new();
    scan(tree, &mut path, &mut facts, &[]);
    facts
}

/// The aggregate function names, in first-seen order with repeats, and whether any GROUP BY
/// appears.
#[must_use]
pub(crate) fn tree_aggregate_facts(tree: &Value) -> (Vec<String>, bool) {
    let facts: TreeFacts = collect_tree_facts(tree);
    (facts.function_names, facts.has_group_by)
}

fn scan(node: &Value, path: &mut Path, facts: &mut TreeFacts, visible_ctes: &[String]) {
    match node {
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                if is_nested(item) {
                    path.push(Step::Index(index));
                    scan(item, path, facts, visible_ctes);
                    path.pop();
                }
            }
        }
        Value::Object(entries) => {
            if let Some(table) = object(entries.get("table"))
                && table.contains_key("schema")
            {
                if let Some(name) = identifier_name(table.get("name"))
                    && !visible_ctes.iter().any(|cte| cte == name)
                    && facts.raw_relation.is_none()
                {
                    facts.raw_relation = Some(name.to_owned());
                }
                return;
            }
            if entries
                .get("group_by")
                .is_some_and(|value| !value.is_null())
            {
                facts.has_group_by = true;
            }
            for (key, payload) in entries {
                if let Some(name) = aggregate_key_name(key, payload)
                    && !name.is_empty()
                {
                    facts.function_names.push(name.to_owned());
                }
                if !is_nested(payload) {
                    continue;
                }
                let child_ctes: Option<Vec<String>> = payload
                    .as_object()
                    .filter(|select| select.contains_key("with"))
                    .and_then(|select| with_ctes(visible_ctes, select));
                path.push(Step::Key(key.clone()));
                scan(
                    payload,
                    path,
                    facts,
                    child_ctes.as_deref().unwrap_or(visible_ctes),
                );
                path.pop();
            }
            if entries.contains_key("from") || entries.contains_key("joins") {
                record_reference_slots(entries, path, facts);
            }
        }
        _ => {}
    }
}

fn record_reference_slots(node: &crate::tree::Object, path: &Path, facts: &mut TreeFacts) {
    if let Some(expressions) = object(node.get("from"))
        .and_then(|from| from.get("expressions"))
        .and_then(Value::as_array)
    {
        for index in 0..expressions.len() {
            let mut slot: Path = path.clone();
            slot.extend([
                Step::Key("from".to_owned()),
                Step::Key("expressions".to_owned()),
                Step::Index(index),
            ]);
            facts.reference_slots.push(slot);
        }
    }
    if let Some(joins) = node.get("joins").and_then(Value::as_array) {
        for (index, join) in joins.iter().enumerate() {
            if join.is_object() {
                let mut slot: Path = path.clone();
                slot.extend([
                    Step::Key("joins".to_owned()),
                    Step::Index(index),
                    Step::Key("this".to_owned()),
                ]);
                facts.reference_slots.push(slot);
            }
        }
    }
}
