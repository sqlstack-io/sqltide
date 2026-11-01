//! Direct build replay decisions: which model each replay rebuilds, the boundary column type
//! it renders with, and the boundaries, lower bounds, and offset progress captured at runtime.

use std::collections::HashMap;

use sqltide_sqltext::python_text::python_isspace;

use crate::virtual_workflow::Mode;
use crate::workflow::step_segment;

/// The replay request fields that select a model and its boundary columns.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Replay {
    pub mode: Mode,
    pub anchor: String,
    pub target: String,
    pub timestamp_column: String,
    pub landed_at_column: String,
    pub cursor_column: String,
}

/// A named relation and its `(column name, type)` pairs, in declaration order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Relation {
    pub name: String,
    pub columns: Vec<(String, String)>,
}

/// Everything replay resolution reads from the plan, desired state, and catalog.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResolveInput {
    /// `(model name, relation names)` of every plan entry, in plan order.
    pub entries: Vec<(String, Vec<String>)>,
    pub replays: Vec<Replay>,
    /// Whether the build forces a start time, which requires existing time lineage.
    pub forced_start_time: bool,
    /// Desired non-Kafka tables, in desired-state order.
    pub desired_tables: Vec<Relation>,
    /// Observed catalog relations, in catalog order.
    pub catalog: Vec<Relation>,
}

/// One replay's model and the steps that capture and consume its boundary.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeReplay {
    pub model_name: String,
    pub capture_step_id: String,
    pub replay_step_id: String,
    pub boundary_column_type: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ResolveError {
    /// No plan entry owns the replay target, or a relation lacks the boundary column.
    Exhausted,
    /// A build error with its user-facing message.
    Build(String),
}

/// Each replay's runtime identity, after checking a forced start time can bound it.
///
/// # Errors
/// Returns [`ResolveError`] in the order the old assembly raised it.
pub fn resolve(input: &ResolveInput) -> Result<Vec<RuntimeReplay>, ResolveError> {
    let model_names: Vec<&str> = input
        .replays
        .iter()
        .map(|replay| model_name(&input.entries, &replay.target))
        .collect::<Result<_, _>>()?;
    if input.forced_start_time {
        check_bounded_inputs(input, &model_names)?;
    }
    input
        .replays
        .iter()
        .zip(model_names)
        .map(|(replay, model_name)| {
            let segment: String = step_segment(model_name);
            Ok(RuntimeReplay {
                model_name: model_name.to_owned(),
                capture_step_id: format!("capture_replay_{segment}"),
                replay_step_id: format!("replay_{segment}"),
                boundary_column_type: boundary_column_type(input, replay)?,
            })
        })
        .collect()
}

fn model_name<'a>(
    entries: &'a [(String, Vec<String>)],
    target: &str,
) -> Result<&'a str, ResolveError> {
    entries
        .iter()
        .find(|(_, names)| names.iter().any(|name| name == target))
        .map(|(name, _)| name.as_str())
        .ok_or(ResolveError::Exhausted)
}

fn relation<'a>(relations: &'a [Relation], name: &str) -> Option<&'a Relation> {
    relations.iter().find(|relation| relation.name == name)
}

fn check_bounded_inputs(input: &ResolveInput, model_names: &[&str]) -> Result<(), ResolveError> {
    // One replay per model name, the last one winning at the first one's position.
    let mut order: Vec<&str> = Vec::new();
    let mut replay_by_model: HashMap<&str, &Replay> = HashMap::new();
    for (replay, model_name) in input.replays.iter().zip(model_names) {
        if replay_by_model.insert(model_name, replay).is_none() {
            order.push(model_name);
        }
    }
    for model_name in order {
        let replay: &Replay = replay_by_model[model_name];
        let Some(relation) = relation(&input.catalog, &replay.anchor) else {
            return Err(ResolveError::Build(format!(
                "Direct --start-time requires existing replay input '{}' for model \
                 '{model_name}'; run an ordinary direct build first to create managed sources \
                 and retained lineage.",
                replay.anchor
            )));
        };
        let time_column: &str = match replay.mode {
            Mode::Offsets if replay.landed_at_column.is_empty() => &replay.timestamp_column,
            Mode::Offsets | Mode::LandedAt => &replay.landed_at_column,
            Mode::Cursor | Mode::Timestamp => &replay.timestamp_column,
        };
        if !relation.columns.iter().any(|(name, _)| name == time_column) {
            return Err(ResolveError::Build(format!(
                "Direct --start-time cannot bound model '{model_name}' because replay input \
                 '{}' does not expose time-lineage column '{time_column}'. Project replay \
                 timestamp or landed-at lineage through the intermediate model before \
                 selecting this closure.",
                replay.anchor
            )));
        }
    }
    Ok(())
}

fn boundary_column_type(
    input: &ResolveInput,
    replay: &Replay,
) -> Result<Option<String>, ResolveError> {
    let position_column: &str = match replay.mode {
        Mode::Offsets => return Ok(None),
        Mode::Cursor => &replay.cursor_column,
        Mode::Timestamp => &replay.timestamp_column,
        Mode::LandedAt => &replay.landed_at_column,
    };
    let relation: &Relation = relation(&input.desired_tables, &replay.anchor)
        .or_else(|| relation(&input.catalog, &replay.anchor))
        .ok_or_else(|| {
            ResolveError::Build(format!(
                "Cannot resolve boundary type for {}.{position_column}",
                replay.anchor
            ))
        })?;
    relation
        .columns
        .iter()
        .find(|(name, _)| name == position_column)
        .map(|(_, column_type)| Some(column_type.clone()))
        .ok_or(ResolveError::Exhausted)
}

/// One retained interval captured for a replay root.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapturedRange {
    pub partition: Option<String>,
    pub lower: String,
    pub cutoff: String,
    pub cutoff_inclusive: bool,
}

/// One replay cutoff, scoped to a partition for offset replays.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Boundary {
    pub key: String,
    pub cutoff: String,
    pub cutoff_inclusive: bool,
    pub partition: Option<String>,
}

/// One replay lower bound, scoped to a partition for offset replays.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LowerBound {
    pub value: String,
    pub partition: Option<String>,
}

/// One partition's captured offset span, for approximate replay progress.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct OffsetRange {
    pub partition: i128,
    pub lower: i128,
    pub upper: i128,
}

/// The replay inputs realized from one capture.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Realized {
    pub boundaries: Vec<Boundary>,
    pub lower_bounds: Vec<LowerBound>,
    /// Empty when the replay has no offset progress to observe.
    pub offset_ranges: Vec<OffsetRange>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CaptureError {
    /// An offset range has no partition.
    MissingPartition,
    /// A value that must be a base-10 integer is not one.
    NotInteger(String),
}

/// Boundaries, lower bounds, and offset progress realized from `ranges` captured in `mode`.
///
/// # Errors
/// Returns [`CaptureError`] in the order the old realization raised it.
pub fn realize(mode: &str, ranges: &[CapturedRange]) -> Result<Realized, CaptureError> {
    if mode != "offsets" {
        return Ok(Realized {
            boundaries: ranges
                .first()
                .map(|range| Boundary {
                    key: format!("_replay_{mode}"),
                    cutoff: range.cutoff.clone(),
                    cutoff_inclusive: range.cutoff_inclusive,
                    partition: None,
                })
                .into_iter()
                .collect(),
            lower_bounds: ranges
                .first()
                .map(|range| LowerBound {
                    value: range.lower.clone(),
                    partition: None,
                })
                .into_iter()
                .collect(),
            offset_ranges: Vec::new(),
        });
    }
    let cutoffs = fold_by_partition(ranges, |range| &range.cutoff, i128::max)?;
    let boundaries: Vec<Boundary> = sorted_by_partition(cutoffs)?
        .into_iter()
        .map(|(partition, cutoff)| Boundary {
            key: format!("_replay_partition={partition}"),
            cutoff: cutoff.to_string(),
            cutoff_inclusive: true,
            partition: Some(partition.to_owned()),
        })
        .collect();
    let lowers = fold_by_partition(ranges, |range| &range.lower, i128::min)?;
    let lower_bounds: Vec<LowerBound> = sorted_by_partition(lowers)?
        .into_iter()
        .map(|(partition, lower)| LowerBound {
            value: lower.to_string(),
            partition: Some(partition.to_owned()),
        })
        .collect();
    Ok(Realized {
        boundaries,
        lower_bounds,
        offset_ranges: offset_ranges(ranges)?,
    })
}

/// `(partition, folded value)` in first-seen partition order.
fn fold_by_partition<'a>(
    ranges: &'a [CapturedRange],
    value: impl Fn(&'a CapturedRange) -> &'a String,
    fold: fn(i128, i128) -> i128,
) -> Result<Vec<(&'a str, i128)>, CaptureError> {
    let mut folded: Vec<(&str, i128)> = Vec::new();
    for range in ranges {
        let partition: &str = range
            .partition
            .as_deref()
            .ok_or(CaptureError::MissingPartition)?;
        let parsed: i128 = python_int(value(range))?;
        match folded.iter_mut().find(|(seen, _)| *seen == partition) {
            Some((_, current)) => *current = fold(parsed, *current),
            None => folded.push((partition, parsed)),
        }
    }
    Ok(folded)
}

/// `values` stably sorted by their partition's integer value.
fn sorted_by_partition(values: Vec<(&str, i128)>) -> Result<Vec<(&str, i128)>, CaptureError> {
    let mut keyed: Vec<(i128, (&str, i128))> = values
        .into_iter()
        .map(|entry| Ok((python_int(entry.0)?, entry)))
        .collect::<Result<_, _>>()?;
    keyed.sort_by_key(|(order, _)| *order);
    Ok(keyed.into_iter().map(|(_, entry)| entry).collect())
}

fn offset_ranges(ranges: &[CapturedRange]) -> Result<Vec<OffsetRange>, CaptureError> {
    let mut spans: Vec<OffsetRange> = Vec::new();
    for range in ranges {
        let partition: i128 = python_int(
            range
                .partition
                .as_deref()
                .ok_or(CaptureError::MissingPartition)?,
        )?;
        let lower: i128 = python_int(&range.lower)?;
        let upper: i128 = python_int(&range.cutoff)?;
        match spans.iter_mut().find(|span| span.partition == partition) {
            Some(span) => {
                span.lower = span.lower.min(lower);
                span.upper = span.upper.max(upper);
            }
            None => spans.push(OffsetRange {
                partition,
                lower,
                upper,
            }),
        }
    }
    spans.sort_by_key(|span| span.partition);
    Ok(spans)
}

/// `text` parsed like Python's `int(text)` for ASCII digits: surrounding whitespace, an
/// optional sign, and single underscores between digits.
fn python_int(text: &str) -> Result<i128, CaptureError> {
    let not_integer = || CaptureError::NotInteger(text.to_owned());
    // `int()` strips what `str.isspace` accepts except the ASCII separators U+001C..U+001F.
    let trimmed: &str =
        text.trim_matches(|c: char| python_isspace(c) && !('\u{1c}'..='\u{1f}').contains(&c));
    let (negative, digits) = match trimmed.as_bytes().first() {
        Some(b'-') => (true, &trimmed[1..]),
        Some(b'+') => (false, &trimmed[1..]),
        _ => (false, trimmed),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'_')
    {
        return Err(not_integer());
    }
    let magnitude: i128 = digits
        .bytes()
        .filter(u8::is_ascii_digit)
        .try_fold(0_i128, |value, digit| {
            value.checked_mul(10)?.checked_add(i128::from(digit - b'0'))
        })
        .ok_or_else(not_integer)?;
    Ok(if negative { -magnitude } else { magnitude })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn range(partition: Option<&str>, lower: &str, cutoff: &str) -> CapturedRange {
        CapturedRange {
            partition: partition.map(str::to_owned),
            lower: lower.to_owned(),
            cutoff: cutoff.to_owned(),
            cutoff_inclusive: false,
        }
    }

    #[test]
    fn offset_captures_fold_per_partition_and_sort_numerically() {
        let realized: Realized = realize(
            "offsets",
            &[
                range(Some("10"), "7", "9"),
                range(Some("2"), "5", "8"),
                range(Some("10"), "3", "12"),
                range(Some("02"), "1", "4"),
            ],
        )
        .unwrap();
        let partitions: Vec<(Option<&str>, &str)> = realized
            .boundaries
            .iter()
            .map(|boundary| (boundary.partition.as_deref(), boundary.cutoff.as_str()))
            .collect();
        // "2" and "02" stay distinct boundaries but sort as equal, in first-seen order.
        assert_eq!(
            partitions,
            [(Some("2"), "8"), (Some("02"), "4"), (Some("10"), "12")]
        );
        assert!(realized.boundaries.iter().all(|b| b.cutoff_inclusive));
        assert_eq!(realized.boundaries[2].key, "_replay_partition=10");
        let lowers: Vec<&str> = realized
            .lower_bounds
            .iter()
            .map(|bound| bound.value.as_str())
            .collect();
        assert_eq!(lowers, ["5", "1", "3"]);
        // Progress merges partitions by integer value.
        assert_eq!(
            realized.offset_ranges,
            [
                OffsetRange {
                    partition: 2,
                    lower: 1,
                    upper: 8
                },
                OffsetRange {
                    partition: 10,
                    lower: 3,
                    upper: 12
                },
            ]
        );
    }

    #[test]
    fn position_captures_use_only_the_first_range() {
        let realized: Realized =
            realize("cursor", &[range(None, "a", "b"), range(None, "c", "d")]).unwrap();
        assert_eq!(
            realized.boundaries,
            [Boundary {
                key: "_replay_cursor".to_owned(),
                cutoff: "b".to_owned(),
                cutoff_inclusive: false,
                partition: None,
            }]
        );
        assert_eq!(realized.lower_bounds[0].value, "a");
        assert!(realized.offset_ranges.is_empty());
        assert_eq!(realize("timestamp", &[]).unwrap(), Realized::default());
    }

    #[test]
    fn offset_captures_reject_missing_partitions_and_non_integers() {
        assert_eq!(
            realize("offsets", &[range(None, "1", "2")]),
            Err(CaptureError::MissingPartition)
        );
        assert_eq!(
            realize("offsets", &[range(Some("x"), "1", "2")]),
            Err(CaptureError::NotInteger("x".to_owned()))
        );
        assert_eq!(
            realize("offsets", &[range(Some("1"), "1_", "2")]),
            Err(CaptureError::NotInteger("1_".to_owned()))
        );
    }

    #[test]
    fn parses_integers_like_python() {
        assert_eq!(python_int(" -1_000\n"), Ok(-1000));
        assert_eq!(python_int("+007"), Ok(7));
        for text in ["", "-", "1__0", "_1", "1.0", "0x1", "١", "\u{1c}5"] {
            assert!(python_int(text).is_err(), "{text:?}");
        }
    }

    fn table(name: &str, columns: &[(&str, &str)]) -> Relation {
        Relation {
            name: name.to_owned(),
            columns: columns
                .iter()
                .map(|(name, column_type)| ((*name).to_owned(), (*column_type).to_owned()))
                .collect(),
        }
    }

    fn replay(mode: Mode, anchor: &str, target: &str) -> Replay {
        Replay {
            mode,
            anchor: anchor.to_owned(),
            target: target.to_owned(),
            timestamp_column: "ts".to_owned(),
            landed_at_column: String::new(),
            cursor_column: "id".to_owned(),
        }
    }

    #[test]
    fn resolves_models_steps_and_boundary_types() {
        let input = ResolveInput {
            entries: vec![
                ("orders.daily".to_owned(), vec!["tbl__daily".to_owned()]),
                ("other".to_owned(), vec!["tbl__daily".to_owned()]),
            ],
            replays: vec![
                replay(Mode::Cursor, "src", "tbl__daily"),
                replay(Mode::Offsets, "src", "tbl__daily"),
            ],
            forced_start_time: true,
            // A desired table takes precedence over the observed catalog relation.
            desired_tables: vec![table("src", &[("id", "UInt64")])],
            catalog: vec![table("src", &[("id", "Int8"), ("ts", "DateTime")])],
        };
        assert_eq!(
            resolve(&input).unwrap(),
            [
                RuntimeReplay {
                    model_name: "orders.daily".to_owned(),
                    capture_step_id: "capture_replay_orders_daily".to_owned(),
                    replay_step_id: "replay_orders_daily".to_owned(),
                    boundary_column_type: Some("UInt64".to_owned()),
                },
                RuntimeReplay {
                    model_name: "orders.daily".to_owned(),
                    capture_step_id: "capture_replay_orders_daily".to_owned(),
                    replay_step_id: "replay_orders_daily".to_owned(),
                    boundary_column_type: None,
                },
            ]
        );
    }

    #[test]
    fn forced_start_time_checks_the_last_replay_of_each_model() {
        let mut first = replay(Mode::LandedAt, "src", "t");
        first.landed_at_column = "landed".to_owned();
        let input = ResolveInput {
            entries: vec![("m".to_owned(), vec!["t".to_owned()])],
            // The first replay would fail, but the second replaces it for model `m`.
            replays: vec![first, replay(Mode::Timestamp, "src", "t")],
            forced_start_time: true,
            desired_tables: vec![],
            catalog: vec![table("src", &[("ts", "DateTime")])],
        };
        let resolved = resolve(&input);
        // The landed-at replay then fails resolving its own boundary type.
        assert_eq!(resolved, Err(ResolveError::Exhausted));
        let input = ResolveInput {
            replays: vec![replay(Mode::Offsets, "missing", "t")],
            ..input
        };
        assert!(matches!(
            resolve(&input),
            Err(ResolveError::Build(message)) if message.contains("requires existing replay input 'missing' for model 'm'")
        ));
    }
}
