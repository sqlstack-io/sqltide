//! Replay requests for population roots: which relations each root reads and writes, the
//! boundaries and lower edge of its replay window, and how bounded replay falls back.

use std::collections::{HashMap, HashSet};

const PARTITION_COLUMN: &str = "_replay_partition";
const OFFSET_COLUMN: &str = "_replay_offset";
const TIMESTAMP_COLUMN: &str = "_replay_timestamp";
const LANDED_AT_COLUMN: &str = "_replay_landed_at";
const CURSOR_COLUMN: &str = "_replay_cursor";
/// The cutoff of a full-replay template, before real watermarks are captured.
const TEMPLATE_CUTOFF: &str = "0";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoundaryMode {
    Offsets,
    Timestamp,
    LandedAt,
    Cursor,
}

impl BoundaryMode {
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "offsets" => Self::Offsets,
            "timestamp" => Self::Timestamp,
            "landed_at" => Self::LandedAt,
            "cursor" => Self::Cursor,
            _ => return None,
        })
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Offsets => "offsets",
            Self::Timestamp => "timestamp",
            Self::LandedAt => "landed_at",
            Self::Cursor => "cursor",
        }
    }

    /// The watermark key a full-replay template uses before any boundary is captured.
    #[must_use]
    pub const fn template_boundary_key(self) -> &'static str {
        match self {
            Self::Offsets => "_replay_partition=0",
            Self::Timestamp => TIMESTAMP_COLUMN,
            Self::LandedAt => LANDED_AT_COLUMN,
            Self::Cursor => CURSOR_COLUMN,
        }
    }

    /// Lineage columns a live table needs to seed history before a bounded replay.
    #[must_use]
    pub const fn history_columns(self) -> &'static [&'static str] {
        match self {
            Self::Offsets => &[PARTITION_COLUMN, OFFSET_COLUMN],
            Self::Timestamp => &[TIMESTAMP_COLUMN],
            Self::LandedAt => &[LANDED_AT_COLUMN],
            Self::Cursor => &[CURSOR_COLUMN],
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionMode {
    Full,
    SeededBounded,
    UnseededBounded,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LowerBoundMode {
    None,
    ForcedTime,
    Lookback,
    ActiveFrontier,
}

impl LowerBoundMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::ForcedTime => "forced_time",
            Self::Lookback => "lookback",
            Self::ActiveFrontier => "active_frontier",
        }
    }
}

/// One population root, with keys as indexes the caller interned.
pub struct Root {
    pub key: usize,
    pub anchor: usize,
    pub mode: BoundaryMode,
    pub execution_mode: ExecutionMode,
    pub forced_start_time: Option<String>,
    pub lookback_seconds: Option<i64>,
}

/// One desired table a root can replay into.
pub struct Table {
    pub key: usize,
    pub name: String,
    pub database: Option<String>,
    pub column_names: Vec<String>,
    pub replay_settings: Vec<(String, String)>,
}

/// One desired materialized view, which feeds the table named `target`.
pub struct View {
    pub target: String,
    pub source: String,
}

/// Replay column overrides an adopted external source declares.
#[derive(Default)]
pub struct ExternalColumns {
    pub partition: Option<String>,
    pub offset: Option<String>,
    pub timestamp: Option<String>,
    pub landed_at: Option<String>,
    pub cursor: Option<String>,
}

/// `(root key, boundary key, cutoff value)`.
pub type Watermark = (usize, String, String);

pub struct PopulationInput<'a> {
    pub roots: &'a [Root],
    pub tables: &'a [Table],
    pub views: &'a [View],
    /// Name of each interned key, used when a key has no physical name.
    pub key_names: &'a [String],
    /// `(key, physical name)`, where a later entry replaces an earlier one.
    pub physical_names: &'a [(usize, String)],
    /// `(anchor key, columns)`, where the first entry for a key wins.
    pub external_columns: &'a [(usize, ExternalColumns)],
    pub replay_anchor_keys: &'a HashSet<usize>,
    pub default_database: &'a str,
    pub boundary_time: &'a str,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Boundary {
    pub boundary_key: String,
    pub cutoff_value: String,
    pub partition_value: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct ReplayPlan {
    pub root: usize,
    pub mode: BoundaryMode,
    pub database: String,
    /// `(root, source, anchor, target)` relation names.
    pub relations: (String, String, String, String),
    /// The view whose query the replay reads, as an index into the input views.
    pub view: usize,
    pub boundaries: Vec<Boundary>,
    /// Partition, offset, timestamp, landed-at, and cursor columns.
    pub columns: [String; 5],
    pub lower_bound_mode: LowerBoundMode,
    pub boundary_time: String,
    pub forced_start_time: Option<String>,
    pub lookback_seconds: Option<i64>,
    pub seeds_history: bool,
    pub target_column_names: Vec<String>,
    pub settings: Vec<(String, String)>,
    pub filter_boundaries_at_source: bool,
}

/// Physical names of every desired table, for rewriting replay queries.
#[must_use]
pub fn physical_table_mappings(input: &PopulationInput<'_>) -> Vec<(String, String)> {
    let physical: HashMap<usize, &str> = input
        .physical_names
        .iter()
        .map(|(key, name)| (*key, name.as_str()))
        .collect();
    input
        .tables
        .iter()
        .map(|table| {
            let name = physical.get(&table.key).copied().unwrap_or(&table.name);
            (table.name.clone(), name.to_owned())
        })
        .collect()
}

/// One full-replay watermark per root, so a request can be built before boundaries exist.
#[must_use]
pub fn template_watermarks(roots: &[Root]) -> Vec<Watermark> {
    roots
        .iter()
        .map(|root| {
            (
                root.key,
                root.mode.template_boundary_key().to_owned(),
                TEMPLATE_CUTOFF.to_owned(),
            )
        })
        .collect()
}

fn lower_bound_mode(root: &Root) -> LowerBoundMode {
    if root.forced_start_time.is_some() {
        LowerBoundMode::ForcedTime
    } else if root.lookback_seconds.is_some() {
        LowerBoundMode::Lookback
    } else if root.execution_mode == ExecutionMode::Full {
        LowerBoundMode::None
    } else {
        LowerBoundMode::ActiveFrontier
    }
}

fn replay_columns(external: Option<&ExternalColumns>) -> [String; 5] {
    let default = ExternalColumns::default();
    let columns = external.unwrap_or(&default);
    let pick = |value: &Option<String>, fallback: &str| {
        value
            .as_deref()
            .filter(|value| !value.is_empty())
            .unwrap_or(fallback)
            .to_owned()
    };
    let landed_at = columns
        .landed_at
        .as_deref()
        .filter(|value| !value.is_empty())
        .or_else(|| {
            columns
                .timestamp
                .as_deref()
                .filter(|value| !value.is_empty())
        })
        .unwrap_or(LANDED_AT_COLUMN)
        .to_owned();
    [
        pick(&columns.partition, PARTITION_COLUMN),
        pick(&columns.offset, OFFSET_COLUMN),
        pick(&columns.timestamp, TIMESTAMP_COLUMN),
        landed_at,
        pick(&columns.cursor, CURSOR_COLUMN),
    ]
}

/// The replay request of every root that has at least one watermark, in root order.
///
/// # Errors
/// Names the root whose table, feeding view, or physical target the plan lacks.
pub fn replay_plans(
    input: &PopulationInput<'_>,
    watermarks: &[Watermark],
) -> Result<Vec<ReplayPlan>, String> {
    let physical: HashMap<usize, &str> = input
        .physical_names
        .iter()
        .map(|(key, name)| (*key, name.as_str()))
        .collect();
    let tables: HashMap<usize, &Table> = input
        .tables
        .iter()
        .map(|table| (table.key, table))
        .collect();
    let views: HashMap<&str, usize> = input
        .views
        .iter()
        .enumerate()
        .map(|(index, view)| (view.target.as_str(), index))
        .collect();
    let mut external: HashMap<usize, &ExternalColumns> = HashMap::new();
    for (key, columns) in input.external_columns {
        external.entry(*key).or_insert(columns);
    }
    let mut plans = Vec::new();
    for root in input.roots {
        let boundaries: Vec<Boundary> = watermarks
            .iter()
            .filter(|(key, _, _)| *key == root.key)
            .map(|(_, boundary_key, cutoff_value)| Boundary {
                boundary_key: boundary_key.clone(),
                cutoff_value: cutoff_value.clone(),
                partition_value: (root.mode == BoundaryMode::Offsets).then(|| {
                    boundary_key
                        .split_once('=')
                        .map_or_else(String::new, |(_, partition)| partition.to_owned())
                }),
            })
            .collect();
        if boundaries.is_empty() {
            continue;
        }
        let root_name = &input.key_names[root.key];
        let table = tables
            .get(&root.key)
            .ok_or_else(|| format!("Population root '{root_name}' has no desired table"))?;
        let view = *views
            .get(table.name.as_str())
            .ok_or_else(|| format!("Population root '{root_name}' has no feeding view"))?;
        let target = physical
            .get(&root.key)
            .ok_or_else(|| format!("Population root '{root_name}' has no physical target"))?;
        let anchor = physical
            .get(&root.anchor)
            .copied()
            .unwrap_or(&input.key_names[root.anchor]);
        let mut settings = table.replay_settings.clone();
        settings.sort();
        plans.push(ReplayPlan {
            root: root.key,
            mode: root.mode,
            database: table
                .database
                .clone()
                .filter(|database| !database.is_empty())
                .unwrap_or_else(|| input.default_database.to_owned()),
            relations: (
                table.name.clone(),
                input.views[view].source.clone(),
                anchor.to_owned(),
                (*target).to_owned(),
            ),
            view,
            boundaries,
            columns: replay_columns(external.get(&root.anchor).copied()),
            lower_bound_mode: lower_bound_mode(root),
            boundary_time: input.boundary_time.to_owned(),
            forced_start_time: root.forced_start_time.clone(),
            lookback_seconds: root.lookback_seconds,
            seeds_history: root.execution_mode == ExecutionMode::SeededBounded,
            target_column_names: table.column_names.clone(),
            settings,
            filter_boundaries_at_source: !input.replay_anchor_keys.contains(&root.key),
        });
    }
    Ok(plans)
}

/// How a seeded bounded rebuild proceeds once the live root table has been inspected.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoundedOutcome {
    /// Seed history and replay the bounded tail as planned.
    Keep,
    /// Replay the bounded tail without seeding history.
    WithoutHistory,
    /// Rebuild the full history instead.
    Full,
}

/// Whether a seeded bounded rebuild of a root can seed history from its live table.
///
/// `live_table` is the physical table the stable view binds and its columns, when it exists. A
/// forced start time without a live table replays the bounded tail alone. Otherwise a root whose
/// live table lacks the mode's lineage columns follows its configured fallback.
#[must_use]
pub fn bounded_outcome(
    mode: Option<BoundaryMode>,
    forced_start_time: bool,
    live_table: Option<&HashSet<String>>,
    fallback_without_history: bool,
) -> BoundedOutcome {
    if live_table.is_none() && forced_start_time {
        return BoundedOutcome::WithoutHistory;
    }
    let required = mode.map_or(&[][..], BoundaryMode::history_columns);
    let supported = required.is_empty()
        || live_table
            .is_some_and(|columns| required.iter().all(|column| columns.contains(*column)));
    match (supported, fallback_without_history) {
        (true, _) => BoundedOutcome::Keep,
        (false, true) => BoundedOutcome::WithoutHistory,
        (false, false) => BoundedOutcome::Full,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input<'a>(
        roots: &'a [Root],
        tables: &'a [Table],
        views: &'a [View],
        names: &'a [String],
        physical: &'a [(usize, String)],
        external: &'a [(usize, ExternalColumns)],
        anchors: &'a HashSet<usize>,
    ) -> PopulationInput<'a> {
        PopulationInput {
            roots,
            tables,
            views,
            key_names: names,
            physical_names: physical,
            external_columns: external,
            replay_anchor_keys: anchors,
            default_database: "analytics",
            boundary_time: "2026-01-01 00:00:00.000",
        }
    }

    #[test]
    fn plans_offset_replays_from_the_anchor_into_the_physical_target() {
        let roots = [Root {
            key: 1,
            anchor: 0,
            mode: BoundaryMode::Offsets,
            execution_mode: ExecutionMode::SeededBounded,
            forced_start_time: None,
            lookback_seconds: None,
        }];
        let tables = [Table {
            key: 1,
            name: "orders".to_owned(),
            database: None,
            column_names: vec!["id".to_owned()],
            replay_settings: vec![
                ("z".to_owned(), "1".to_owned()),
                ("a".to_owned(), "2".to_owned()),
            ],
        }];
        let views = [View {
            target: "orders".to_owned(),
            source: "raw_orders".to_owned(),
        }];
        let names = ["raw_orders".to_owned(), "orders".to_owned()];
        let physical = [(1, "orders__d1".to_owned())];
        let external = [
            (
                0,
                ExternalColumns {
                    timestamp: Some("event_time".to_owned()),
                    partition: Some(String::new()),
                    ..ExternalColumns::default()
                },
            ),
            (0, ExternalColumns::default()),
        ];
        let anchors = HashSet::from([1]);
        let input = input(
            &roots, &tables, &views, &names, &physical, &external, &anchors,
        );
        let watermarks = vec![
            (1, "_replay_partition=3".to_owned(), "41".to_owned()),
            (2, "_replay_partition=0".to_owned(), "9".to_owned()),
        ];
        let plans = replay_plans(&input, &watermarks).unwrap();
        assert_eq!(plans.len(), 1);
        let plan = &plans[0];
        assert_eq!(
            plan.relations,
            (
                "orders".to_owned(),
                "raw_orders".to_owned(),
                "raw_orders".to_owned(),
                "orders__d1".to_owned()
            )
        );
        assert_eq!(plan.boundaries[0].partition_value.as_deref(), Some("3"));
        assert_eq!(
            plan.columns,
            [
                PARTITION_COLUMN,
                OFFSET_COLUMN,
                "event_time",
                "event_time",
                CURSOR_COLUMN
            ]
            .map(str::to_owned)
        );
        assert_eq!(plan.lower_bound_mode, LowerBoundMode::ActiveFrontier);
        assert!(plan.seeds_history && !plan.filter_boundaries_at_source);
        assert_eq!(plan.settings[0].0, "a");
        assert_eq!(plan.database, "analytics");
        assert_eq!(
            physical_table_mappings(&input),
            [("orders".to_owned(), "orders__d1".to_owned())]
        );
        assert_eq!(
            template_watermarks(&roots),
            [(1, "_replay_partition=0".to_owned(), "0".to_owned())]
        );
    }

    #[test]
    fn falls_back_only_when_the_live_table_lacks_lineage_columns() {
        let lineage: HashSet<String> =
            HashSet::from([PARTITION_COLUMN.to_owned(), OFFSET_COLUMN.to_owned()]);
        let partial: HashSet<String> = HashSet::from([PARTITION_COLUMN.to_owned()]);
        let offsets = Some(BoundaryMode::Offsets);
        assert_eq!(
            bounded_outcome(offsets, true, Some(&lineage), false),
            BoundedOutcome::Keep
        );
        assert_eq!(
            bounded_outcome(offsets, false, Some(&partial), true),
            BoundedOutcome::WithoutHistory
        );
        assert_eq!(
            bounded_outcome(offsets, false, Some(&partial), false),
            BoundedOutcome::Full
        );
        assert_eq!(
            bounded_outcome(offsets, false, None, false),
            BoundedOutcome::Full
        );
        assert_eq!(
            bounded_outcome(offsets, true, None, false),
            BoundedOutcome::WithoutHistory
        );
        assert_eq!(
            bounded_outcome(None, false, None, false),
            BoundedOutcome::Keep
        );
    }

    #[test]
    fn prefers_a_forced_start_over_lookback_over_the_frontier() {
        let mut root = Root {
            key: 0,
            anchor: 0,
            mode: BoundaryMode::Cursor,
            execution_mode: ExecutionMode::Full,
            forced_start_time: None,
            lookback_seconds: Some(60),
        };
        assert_eq!(lower_bound_mode(&root), LowerBoundMode::Lookback);
        root.forced_start_time = Some("2026-01-01".to_owned());
        assert_eq!(lower_bound_mode(&root), LowerBoundMode::ForcedTime);
        root.forced_start_time = None;
        root.lookback_seconds = None;
        assert_eq!(lower_bound_mode(&root), LowerBoundMode::None);
        root.execution_mode = ExecutionMode::UnseededBounded;
        assert_eq!(lower_bound_mode(&root), LowerBoundMode::ActiveFrontier);
    }
}
