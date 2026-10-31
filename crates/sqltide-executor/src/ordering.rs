//! Drop order for destructive relation groups: dependants before their dependencies.

use std::cmp::Reverse;
use std::collections::{BTreeSet, BinaryHeap, HashMap};

/// Why relations could not be ordered.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OrderingError {
    /// The position of the first relation whose name repeats an earlier one.
    Duplicate(usize),
    /// The relations left with dependants, sorted by name.
    Cycle(Vec<String>),
}

/// Positions of `relations` (name, dependency names) ordered so each relation precedes its
/// dependencies, ready ties broken by name. Dependencies outside `relations` are ignored.
///
/// # Errors
///
/// Returns [`OrderingError`] for a repeated name or a dependency cycle.
pub fn reverse_topological_order(
    relations: &[(String, Vec<String>)],
) -> Result<Vec<usize>, OrderingError> {
    let mut position_by_name: HashMap<&str, usize> = HashMap::with_capacity(relations.len());
    for (position, (name, _)) in relations.iter().enumerate() {
        if position_by_name.insert(name, position).is_some() {
            return Err(OrderingError::Duplicate(position));
        }
    }
    let dependencies: Vec<Vec<usize>> = relations
        .iter()
        .map(|(_, names)| {
            names
                .iter()
                .filter_map(|name| position_by_name.get(name.as_str()).copied())
                .collect::<BTreeSet<usize>>()
                .into_iter()
                .collect()
        })
        .collect();
    let mut dependant_counts: Vec<usize> = vec![0; relations.len()];
    for &dependency in dependencies.iter().flatten() {
        dependant_counts[dependency] += 1;
    }
    let mut ready: BinaryHeap<Reverse<(&str, usize)>> = dependant_counts
        .iter()
        .enumerate()
        .filter(|&(_, &count)| count == 0)
        .map(|(position, _)| Reverse((relations[position].0.as_str(), position)))
        .collect();
    let mut ordered: Vec<usize> = Vec::with_capacity(relations.len());
    while let Some(Reverse((_, position))) = ready.pop() {
        ordered.push(position);
        for &dependency in &dependencies[position] {
            dependant_counts[dependency] -= 1;
            if dependant_counts[dependency] == 0 {
                ready.push(Reverse((relations[dependency].0.as_str(), dependency)));
            }
        }
    }
    if ordered.len() == relations.len() {
        return Ok(ordered);
    }
    let mut unresolved: Vec<String> = dependant_counts
        .iter()
        .enumerate()
        .filter(|&(_, &count)| count > 0)
        .map(|(position, _)| relations[position].0.clone())
        .collect();
    unresolved.sort();
    Err(OrderingError::Cycle(unresolved))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn relations(entries: &[(&str, &[&str])]) -> Vec<(String, Vec<String>)> {
        entries
            .iter()
            .map(|(name, deps)| {
                (
                    (*name).to_owned(),
                    deps.iter().map(|dep| (*dep).to_owned()).collect(),
                )
            })
            .collect()
    }

    #[test]
    fn drops_dependants_first_with_lexical_ties() {
        let relations = relations(&[
            ("orders", &[]),
            ("orders_mv", &["raw__orders", "orders", "orders"]),
            ("raw__orders", &["external"]),
            ("customers_view", &["orders"]),
        ]);
        assert_eq!(reverse_topological_order(&relations), Ok(vec![3, 1, 0, 2]));
    }

    #[test]
    fn rejects_duplicates_and_cycles() {
        assert_eq!(
            reverse_topological_order(&relations(&[("a", &[]), ("b", &[]), ("a", &[])])),
            Err(OrderingError::Duplicate(2))
        );
        assert_eq!(
            reverse_topological_order(&relations(&[("a", &["b"]), ("b", &["a"]), ("c", &["a"])])),
            Err(OrderingError::Cycle(vec!["a".to_owned(), "b".to_owned()]))
        );
    }
}
