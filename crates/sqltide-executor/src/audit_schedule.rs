//! When each scheduled audit becomes eligible after warmup, and when it is next due.

use std::collections::{HashMap, HashSet};

use crate::timestamps::{Instant, TimestampError, parse_warehouse_timestamp};

/// Last-run statuses that mean the audit's identity changed, so it waits for the next slot.
const IDENTITY_DRIFT_STATUSES: [&str; 3] =
    ["binding_changed", "definition_changed", "execution_changed"];
const DEFERRED_STATUS: &str = "deferred";

/// Whether an audit's referenced models have existed long enough for it to run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Warmup {
    pub eligible: bool,
    /// The newest apply or publication timestamp across the referenced models, as stored.
    pub anchor: Option<String>,
    pub eligible_at: Option<String>,
    pub missing_model_names: Vec<String>,
}

/// The warmup state of one audit. Models with neither an anchor nor a materialized relation are
/// missing; otherwise the audit is eligible `warmup_seconds` after its newest anchor.
pub fn resolve_warmup(
    referenced_model_names: &[String],
    warmup_seconds: i64,
    anchors_by_model: &HashMap<String, String>,
    materialized_model_names: &HashSet<String>,
    now: Instant,
) -> Result<Warmup, TimestampError> {
    let missing_model_names: Vec<String> = referenced_model_names
        .iter()
        .filter(|name| {
            !anchors_by_model.contains_key(*name) && !materialized_model_names.contains(*name)
        })
        .cloned()
        .collect();
    let anchor: Option<String> = referenced_model_names
        .iter()
        .filter_map(|name| anchors_by_model.get(name))
        .max()
        .cloned();
    if !missing_model_names.is_empty() {
        return Ok(Warmup {
            eligible: false,
            anchor,
            eligible_at: None,
            missing_model_names,
        });
    }
    let Some(anchor_text) = anchor.as_deref().filter(|_| warmup_seconds != 0) else {
        return Ok(Warmup {
            eligible: true,
            eligible_at: anchor.clone(),
            anchor,
            missing_model_names,
        });
    };
    let eligible_at: Instant =
        parse_warehouse_timestamp(anchor_text)?.plus_seconds(warmup_seconds)?;
    Ok(Warmup {
        eligible: now >= eligible_at,
        eligible_at: Some(eligible_at.render()),
        anchor,
        missing_model_names,
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScheduleState {
    NotMaterialized,
    WarmingUp,
    Due,
    Scheduled,
}

impl ScheduleState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NotMaterialized => "not_materialized",
            Self::WarmingUp => "warming_up",
            Self::Due => "due",
            Self::Scheduled => "scheduled",
        }
    }
}

/// The audit's last recorded run, when it has one.
pub struct LastRun<'run> {
    pub status: &'run str,
    pub completed_at: Option<&'run str>,
}

/// One audit's schedule inputs, with the parts of its [`Warmup`] the schedule depends on.
pub struct ScheduledAudit<'audit> {
    pub referenced_model_names: &'audit [String],
    pub cadence_seconds: Option<i64>,
    pub has_missing_models: bool,
    pub eligible: bool,
    pub anchored: bool,
    pub eligible_at: Option<&'audit str>,
    pub last_run: Option<LastRun<'audit>>,
}

fn due_or_scheduled(now: Instant, scheduled_for: Instant) -> ScheduleState {
    if now >= scheduled_for {
        ScheduleState::Due
    } else {
        ScheduleState::Scheduled
    }
}

/// The audit's state and the instant it is scheduled for, if any.
///
/// A first run waits for warmup, or for the next cadence slot when nothing anchors it. Later
/// runs follow the cadence from the last completion, rerun once a referenced model is newer
/// than that completion, and wait for the next slot after an identity change.
pub fn schedule(
    audit: &ScheduledAudit<'_>,
    anchors_by_model: &HashMap<String, String>,
    warehouse_now: &str,
) -> Result<(ScheduleState, Option<Instant>), TimestampError> {
    let now: Instant = parse_warehouse_timestamp(warehouse_now)?;
    let eligible_at: Instant = parse_warehouse_timestamp(
        audit
            .eligible_at
            .filter(|value| !value.is_empty())
            .unwrap_or(warehouse_now),
    )?;
    let slot = || {
        now.cadence_slot(
            audit
                .cadence_seconds
                .filter(|seconds| *seconds != 0)
                .unwrap_or(1),
        )
    };
    if audit.has_missing_models {
        return Ok((ScheduleState::NotMaterialized, None));
    }
    if !audit.eligible {
        return Ok((ScheduleState::WarmingUp, Some(eligible_at)));
    }
    let Some(LastRun {
        status,
        completed_at: Some(completed_at),
    }) = audit.last_run
    else {
        let scheduled_for: Instant = if audit.anchored { eligible_at } else { slot()? };
        return Ok((due_or_scheduled(now, scheduled_for), Some(scheduled_for)));
    };
    let completed_at: Instant = parse_warehouse_timestamp(completed_at)?;
    let mut newest_anchor: Option<Instant> = None;
    for name in audit.referenced_model_names {
        if let Some(anchor) = anchors_by_model.get(name) {
            let anchor: Instant = parse_warehouse_timestamp(anchor)?;
            newest_anchor = Some(newest_anchor.map_or(anchor, |newest| newest.max(anchor)));
        }
    }
    let scheduled_for: Instant = if IDENTITY_DRIFT_STATUSES.contains(&status) {
        slot()?.max(eligible_at)
    } else if status == DEFERRED_STATUS || newest_anchor.is_some_and(|anchor| anchor > completed_at)
    {
        eligible_at
    } else {
        completed_at
            .plus_seconds(audit.cadence_seconds.unwrap_or(0))?
            .max(eligible_at)
    };
    Ok((due_or_scheduled(now, scheduled_for), Some(scheduled_for)))
}
