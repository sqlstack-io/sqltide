//! Whether a staged root is ready to publish, judged from adapter readiness observations.
//!
//! Row counts are `i64` and ratios and lags `f64`, matching Python for counts below 2^53.

/// The audit verdict for one staged root.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Assessment {
    Ready,
    NotReady,
    Caution,
}

impl Assessment {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::NotReady => "not_ready",
            Self::Caution => "caution",
        }
    }
}

/// Per-partition offset catch-up between the active and staged tables.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct OffsetSummary {
    pub active_partition_count: i64,
    pub partitions_compared: i64,
    pub missing_staged_partition_count: i64,
    pub missing_freshness_partition_count: i64,
    pub has_lag_boundary_column: bool,
    pub max_lag_seconds: Option<f64>,
}

/// What the adapter observed for one staged root.
#[derive(Clone, Debug, PartialEq)]
pub struct Observation {
    pub root_name: String,
    pub staged_exists: bool,
    pub active_exists: bool,
    pub active_row_count: Option<i64>,
    pub staged_row_count: Option<i64>,
    /// Whether the root replays by source offsets rather than a scalar boundary.
    pub offset_lineage: bool,
    pub offset_summary: Option<OffsetSummary>,
    /// The scalar boundary lag, `None` when there is no scalar summary or it has no lag.
    pub scalar_lag_seconds: Option<f64>,
    pub replay_source_name: Option<String>,
    pub replay_source_row_count: Option<i64>,
}

/// The publish thresholds an audit applies.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Thresholds {
    pub maximum_lag_seconds: f64,
    pub minimum_staged_row_ratio: f64,
}

/// One root's audit outcome.
#[derive(Clone, Debug, PartialEq)]
pub struct Audit {
    pub assessment: Assessment,
    pub row_delta: Option<i64>,
    pub row_ratio: Option<f64>,
    pub warnings: Vec<String>,
}

/// Audit one staged root.
#[must_use]
pub fn audit(observation: &Observation, thresholds: &Thresholds) -> Audit {
    let (row_delta, row_ratio) = match (observation.active_row_count, observation.staged_row_count)
    {
        (Some(active), Some(staged)) => (Some(staged - active), ratio(staged, active)),
        _ => (None, None),
    };
    let mut warnings: Vec<String> = Vec::new();
    if let (Some(source), Some(0)) = (
        &observation.replay_source_name,
        observation.replay_source_row_count,
    ) {
        warnings.push(format!("replay source {source} is empty"));
    }
    if observation.active_exists
        && row_ratio.is_some_and(|ratio| ratio < thresholds.minimum_staged_row_ratio)
    {
        warnings.push(format!(
            "staged row count is far below active row count for {}",
            observation.root_name
        ));
    }
    Audit {
        assessment: assessment(observation, thresholds),
        row_delta,
        row_ratio,
        warnings,
    }
}

fn ratio(staged: i64, active: i64) -> Option<f64> {
    (active != 0).then(|| staged as f64 / active as f64)
}

fn assessment(observation: &Observation, thresholds: &Thresholds) -> Assessment {
    if !observation.staged_exists {
        return Assessment::Caution;
    }
    if observation.staged_row_count == Some(0) {
        return if observation.active_exists && observation.active_row_count.is_some_and(|n| n > 0) {
            Assessment::NotReady
        } else {
            Assessment::Caution
        };
    }
    if !observation.active_exists {
        return Assessment::Ready;
    }
    let lag_seconds: Option<f64> = if observation.offset_lineage {
        let Some(summary) = observation.offset_summary else {
            return Assessment::Caution;
        };
        if summary.partitions_compared < summary.active_partition_count
            || summary.missing_staged_partition_count > 0
            || summary.missing_freshness_partition_count > 0
            || !summary.has_lag_boundary_column
        {
            return Assessment::Caution;
        }
        summary.max_lag_seconds
    } else {
        observation.scalar_lag_seconds
    };
    let Some(lag_seconds) = lag_seconds else {
        return Assessment::Caution;
    };
    if lag_seconds > thresholds.maximum_lag_seconds {
        return Assessment::NotReady;
    }
    row_ratio_assessment(observation, thresholds)
}

fn row_ratio_assessment(observation: &Observation, thresholds: &Thresholds) -> Assessment {
    match (observation.active_row_count, observation.staged_row_count) {
        (Some(active), Some(staged))
            if active > 0
                && (staged as f64) < active as f64 * thresholds.minimum_staged_row_ratio =>
        {
            Assessment::Caution
        }
        _ => Assessment::Ready,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const THRESHOLDS: Thresholds = Thresholds {
        maximum_lag_seconds: 60.0,
        minimum_staged_row_ratio: 0.9,
    };

    fn observation() -> Observation {
        Observation {
            root_name: "orders".to_owned(),
            staged_exists: true,
            active_exists: true,
            active_row_count: Some(100),
            staged_row_count: Some(95),
            offset_lineage: false,
            offset_summary: None,
            scalar_lag_seconds: Some(10.0),
            replay_source_name: None,
            replay_source_row_count: None,
        }
    }

    fn assess(change: impl FnOnce(&mut Observation)) -> Assessment {
        let mut observation: Observation = observation();
        change(&mut observation);
        audit(&observation, &THRESHOLDS).assessment
    }

    #[test]
    fn scalar_lineage_checks_lag_then_row_ratio() {
        assert_eq!(assess(|_| {}), Assessment::Ready);
        assert_eq!(
            assess(|o| o.scalar_lag_seconds = Some(61.0)),
            Assessment::NotReady
        );
        assert_eq!(assess(|o| o.scalar_lag_seconds = None), Assessment::Caution);
        assert_eq!(
            assess(|o| o.staged_row_count = Some(89)),
            Assessment::Caution
        );
    }

    #[test]
    fn empty_staging_is_not_ready_only_over_a_populated_active_table() {
        assert_eq!(
            assess(|o| o.staged_row_count = Some(0)),
            Assessment::NotReady
        );
        assert_eq!(
            assess(|o| {
                o.staged_row_count = Some(0);
                o.active_row_count = Some(0);
            }),
            Assessment::Caution
        );
        assert_eq!(assess(|o| o.active_exists = false), Assessment::Ready);
        assert_eq!(assess(|o| o.staged_exists = false), Assessment::Caution);
    }

    #[test]
    fn offset_lineage_requires_complete_fresh_partitions() {
        let complete: OffsetSummary = OffsetSummary {
            active_partition_count: 3,
            partitions_compared: 3,
            missing_staged_partition_count: 0,
            missing_freshness_partition_count: 0,
            has_lag_boundary_column: true,
            max_lag_seconds: Some(5.0),
        };
        let offsets = |summary: Option<OffsetSummary>| {
            assess(|o| {
                o.offset_lineage = true;
                o.offset_summary = summary;
                o.scalar_lag_seconds = None;
            })
        };
        assert_eq!(offsets(Some(complete)), Assessment::Ready);
        assert_eq!(offsets(None), Assessment::Caution);
        assert_eq!(
            offsets(Some(OffsetSummary {
                partitions_compared: 2,
                ..complete
            })),
            Assessment::Caution
        );
        assert_eq!(
            offsets(Some(OffsetSummary {
                max_lag_seconds: Some(61.0),
                ..complete
            })),
            Assessment::NotReady
        );
    }

    #[test]
    fn warns_on_an_empty_source_and_a_low_row_ratio() {
        let mut observation: Observation = observation();
        observation.staged_row_count = Some(50);
        observation.replay_source_name = Some("raw__orders".to_owned());
        observation.replay_source_row_count = Some(0);
        let audit: Audit = audit(&observation, &THRESHOLDS);
        assert_eq!(audit.row_delta, Some(-50));
        assert_eq!(audit.row_ratio, Some(0.5));
        assert_eq!(
            audit.warnings,
            [
                "replay source raw__orders is empty",
                "staged row count is far below active row count for orders"
            ]
        );
    }
}
