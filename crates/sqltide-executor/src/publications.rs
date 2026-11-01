//! Publication events, their ranks, and the rollback targets chosen from them.

/// `(database, logical name, physical name)` of one stable binding.
pub type Binding = (String, String, String);

/// When a publication happened, as UTC microseconds, then its publication or deployment ID.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Rank {
    pub micros: i64,
    pub id: String,
}

/// One recorded publication of a deployment.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Publication {
    pub deployment_id: String,
    /// `None` when the publication time could not be parsed.
    pub rank: Option<Rank>,
    pub bindings: Vec<Binding>,
}

/// The bindings as a sorted identity, so equal sets compare equal.
#[must_use]
pub fn identity(bindings: &[Binding]) -> Vec<Binding> {
    let mut identity: Vec<Binding> = bindings.to_vec();
    identity.sort();
    identity
}

/// Which publication a rollback restores.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RollbackTarget<'a> {
    /// The latest publication before the current one with different bindings.
    Previous,
    /// The latest publication of this deployment.
    Deployment(&'a str),
}

/// Why no rollback target was chosen.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RollbackError {
    /// The rollback is refused with this message.
    Refused(String),
    /// The publication at this position has to be ranked and its time could not be parsed.
    Unranked(usize),
}

/// `(current, target)` positions in `publications`: the latest publication whose bindings are
/// the active ones, and the publication `target` restores. Ranks are read in the order the
/// choice needs them, so the first unparsed time reported is the first one reached.
///
/// # Errors
///
/// Returns [`RollbackError`] when the rollback is refused or needs an unparsed rank.
pub fn select_rollback(
    publications: &[Publication],
    active: &[Binding],
    target: RollbackTarget<'_>,
) -> Result<(usize, usize), RollbackError> {
    if active.is_empty() {
        return Err(refused("Rollback requires an active published deployment"));
    }
    let active_identity: Vec<Binding> = identity(active);
    let current: usize = latest(
        publications,
        (0..publications.len())
            .filter(|&position| identity(&publications[position].bindings) == active_identity),
    )?
    .ok_or_else(|| {
        refused(
            "Active bindings do not match a complete publication; repair the target before rollback",
        )
    })?;
    let current_publication: &Publication = &publications[current];
    let target: usize = match target {
        RollbackTarget::Previous => {
            let current_rank: &Rank = rank(publications, current)?;
            let current_identity: Vec<Binding> = identity(&current_publication.bindings);
            let mut earlier: Vec<usize> = Vec::new();
            for (position, publication) in publications.iter().enumerate() {
                if rank(publications, position)? < current_rank
                    && identity(&publication.bindings) != current_identity
                {
                    earlier.push(position);
                }
            }
            latest(publications, earlier.into_iter())?.ok_or_else(|| {
                refused("No previous published deployment is available for rollback")
            })?
        }
        RollbackTarget::Deployment(deployment_id) => {
            if deployment_id == current_publication.deployment_id {
                return Err(refused(&format!(
                    "Deployment '{deployment_id}' is already active"
                )));
            }
            latest(
                publications,
                (0..publications.len())
                    .filter(|&position| publications[position].deployment_id == deployment_id),
            )?
            .ok_or_else(|| {
                refused(&format!(
                    "Deployment '{deployment_id}' has no successful publication to roll back to"
                ))
            })?
        }
    };
    Ok((current, target))
}

/// The first of `positions` with the greatest rank.
fn latest(
    publications: &[Publication],
    positions: impl Iterator<Item = usize>,
) -> Result<Option<usize>, RollbackError> {
    let mut best: Option<(usize, &Rank)> = None;
    for position in positions {
        let candidate: &Rank = rank(publications, position)?;
        if best.is_none_or(|(_, best_rank)| candidate > best_rank) {
            best = Some((position, candidate));
        }
    }
    Ok(best.map(|(position, _)| position))
}

fn rank(publications: &[Publication], position: usize) -> Result<&Rank, RollbackError> {
    publications[position]
        .rank
        .as_ref()
        .ok_or(RollbackError::Unranked(position))
}

fn refused(message: &str) -> RollbackError {
    RollbackError::Refused(message.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn binding(physical: &str) -> Binding {
        ("db".to_owned(), "orders".to_owned(), physical.to_owned())
    }

    fn publication(deployment_id: &str, micros: Option<i64>, physical: &str) -> Publication {
        Publication {
            deployment_id: deployment_id.to_owned(),
            rank: micros.map(|micros| Rank {
                micros,
                id: deployment_id.to_owned(),
            }),
            bindings: vec![binding(physical)],
        }
    }

    #[test]
    fn previous_skips_republications_of_the_current_bindings() {
        let publications: Vec<Publication> = vec![
            publication("d1", Some(1), "orders__d1"),
            publication("d2", Some(2), "orders__d2"),
            publication("d2", Some(3), "orders__d2"),
            publication("d1", Some(4), "orders__d1"),
        ];
        let active: Vec<Binding> = vec![binding("orders__d1")];
        assert_eq!(
            select_rollback(&publications, &active, RollbackTarget::Previous),
            Ok((3, 2))
        );
        assert_eq!(
            select_rollback(&publications, &active, RollbackTarget::Deployment("d2")),
            Ok((3, 2))
        );
        assert_eq!(
            select_rollback(&publications, &active, RollbackTarget::Deployment("d1")),
            Err(RollbackError::Refused(
                "Deployment 'd1' is already active".to_owned()
            ))
        );
    }

    #[test]
    fn unparsed_times_fail_only_when_ranked() {
        let publications: Vec<Publication> = vec![
            publication("d0", None, "orders__d0"),
            publication("d1", Some(4), "orders__d1"),
        ];
        let active: Vec<Binding> = vec![binding("orders__d1")];
        assert_eq!(
            select_rollback(&publications, &active, RollbackTarget::Deployment("d9")),
            Err(RollbackError::Refused(
                "Deployment 'd9' has no successful publication to roll back to".to_owned()
            ))
        );
        assert_eq!(
            select_rollback(&publications, &active, RollbackTarget::Previous),
            Err(RollbackError::Unranked(0))
        );
    }
}
