//! Order the real compiled models one SQL test must assemble to reach a logical relation.

use std::collections::{HashMap, HashSet};

/// Why a relation cannot be assembled.
#[derive(Debug, PartialEq, Eq)]
pub enum ChainError {
    /// The named model depends on itself through its references.
    Cycle(String),
    /// The named relation is neither mocked nor a compiled model.
    Unresolved(String),
}

/// The models newly assembled, in dependency order, and each mock reached, in visit order.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ChainResolution {
    pub assembled: Vec<String>,
    pub used_mocks: Vec<String>,
}

/// Resolve `logical_name` against mocked relations, models assembled by earlier calls, and the
/// compiled models' references, visiting references depth-first in authored order.
///
/// A mock always wins over a model of the same name, and reaching it marks it used.
///
/// # Errors
///
/// Returns [`ChainError`] for the first cycle or unresolvable relation reached.
pub fn resolve_chain<S: std::hash::BuildHasher>(
    logical_name: &str,
    refs_by_model: &HashMap<String, Vec<String>, S>,
    mocks: &HashSet<String, S>,
    assembled: &HashSet<String, S>,
) -> Result<ChainResolution, ChainError> {
    let mut resolution = ChainResolution::default();
    let mut newly_assembled: HashSet<&str> = HashSet::new();
    let mut active: HashSet<&str> = HashSet::new();
    let mut pending: Vec<(&str, bool)> = vec![(logical_name, false)];
    while let Some((name, expanded)) = pending.pop() {
        if mocks.contains(name) {
            resolution.used_mocks.push(name.to_owned());
            continue;
        }
        if assembled.contains(name) || newly_assembled.contains(name) {
            continue;
        }
        if expanded {
            newly_assembled.insert(name);
            resolution.assembled.push(name.to_owned());
            active.remove(name);
            continue;
        }
        if active.contains(name) {
            return Err(ChainError::Cycle(name.to_owned()));
        }
        let Some(refs) = refs_by_model.get(name) else {
            return Err(ChainError::Unresolved(name.to_owned()));
        };
        active.insert(name);
        pending.push((name, true));
        pending.extend(
            refs.iter()
                .rev()
                .map(|reference| (reference.as_str(), false)),
        );
    }
    Ok(resolution)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry(entries: &[(&str, &[&str])]) -> HashMap<String, Vec<String>> {
        entries
            .iter()
            .map(|(name, refs)| {
                (
                    (*name).to_owned(),
                    refs.iter()
                        .map(|reference| (*reference).to_owned())
                        .collect(),
                )
            })
            .collect()
    }

    #[test]
    fn assembles_dependencies_first_in_authored_order_and_prefers_mocks() {
        let models = registry(&[
            ("c", &["b", "a", "src"]),
            ("b", &["a"]),
            ("a", &[]),
            ("src", &[]),
        ]);
        let mocks: HashSet<String> = HashSet::from(["src".to_owned()]);
        assert_eq!(
            resolve_chain("c", &models, &mocks, &HashSet::new()),
            Ok(ChainResolution {
                assembled: vec!["a".to_owned(), "b".to_owned(), "c".to_owned()],
                used_mocks: vec!["src".to_owned()],
            })
        );
    }

    #[test]
    fn reports_cycles_and_unresolved_relations() {
        let models = registry(&[("a", &["b"]), ("b", &["a"]), ("c", &["missing"])]);
        let none: HashSet<String> = HashSet::new();
        assert_eq!(
            resolve_chain("a", &models, &none, &none),
            Err(ChainError::Cycle("a".to_owned()))
        );
        assert_eq!(
            resolve_chain("c", &models, &none, &none),
            Err(ChainError::Unresolved("missing".to_owned()))
        );
    }
}
