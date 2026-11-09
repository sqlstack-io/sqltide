//! Text and JSON rendering of one virtual deployment plan for `sqt plan`.

use sqltide_json::{JsonDialect, JsonInteger, JsonValue, StdlibJsonOptions, dumps};

use crate::cli_style::CliStyle;

const TABLE: &str = "table";
const VIEW: &str = "view";
const RAW_TABLE_NAME_PREFIX: &str = "raw__";
const CREATE: &str = "create";
const REPLACE: &str = "replace";
const REBUILD: &str = "rebuild";
const NO_OP: &str = "no_op";
const FULL_REBUILD: &str = "full_rebuild";
const SEEDED_BOUNDED_REBUILD: &str = "seeded_bounded_rebuild";
const FALLBACK_FULL: &str = "full";
const FALLBACK_BOUNDED_WITHOUT_HISTORY: &str = "bounded_without_history";
const VIRTUAL_ENVIRONMENTS_MODE_LABEL: &str = "virtual environments";

/// One object identity, compared on all three parts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Key {
    pub database: Option<String>,
    pub object_type: String,
    pub name: String,
}

pub struct Change {
    pub key: Key,
    pub change_type: String,
    pub force_full_refresh: bool,
    pub forced_start_time: Option<String>,
    pub schema_change_kind: Option<String>,
    pub seed_compatibility: Option<String>,
}

pub struct Subtree {
    pub root_key: Key,
    pub affected_keys: Vec<Key>,
    pub upstream_boundary_key: Key,
    pub strategy: String,
    pub replay_required: bool,
    pub execution_mode: String,
    pub forced_full_refresh: bool,
    pub forced_start_time: Option<String>,
    pub requested_start_time: Option<String>,
    pub configured_backfill_mode: Option<String>,
    pub execution_lookback_seconds: Option<i64>,
    pub history_preserving_bounded_supported: bool,
    pub resolved_bounded_replay_fallback: Option<String>,
}

pub struct Step {
    pub step_id: String,
    pub phase: String,
    pub action: String,
    pub root_key: Key,
    pub target_key: Option<Key>,
    pub physical_name: Option<String>,
}

pub struct ShadowObject {
    pub logical_key: Key,
    pub physical_name: String,
    pub logical_model_name: String,
}

pub struct Warning {
    pub warning_code: String,
    pub message: String,
    pub root_key: Key,
    pub target_key: Option<Key>,
}

pub struct SqlDiff {
    pub key: Key,
    pub object_type: String,
    pub name: String,
    pub diff_lines: Vec<String>,
}

pub struct Plan {
    pub deployment_id: Option<String>,
    pub object_changes: Vec<Change>,
    pub rebuild_subtrees: Vec<Subtree>,
    pub steps: Vec<Step>,
    pub prepared_shadow_objects: Vec<ShadowObject>,
    pub warnings: Vec<Warning>,
    pub sql_diffs: Vec<SqlDiff>,
}

/// The parts of a desired table, materialized view or view that plan text reads.
pub enum Desired {
    Table,
    MaterializedView { target_table_name: String },
    View,
}

/// Desired tables, materialized views and views by key, the last object winning a key.
pub struct DesiredObjects {
    objects: Vec<(Key, Desired)>,
}

impl DesiredObjects {
    pub fn new(objects: Vec<(Key, Desired)>) -> Self {
        let mut unique: Vec<(Key, Desired)> = Vec::new();
        for (key, desired) in objects {
            match unique.iter_mut().find(|(known, _)| *known == key) {
                Some(slot) => slot.1 = desired,
                None => unique.push((key, desired)),
            }
        }
        Self { objects: unique }
    }

    fn get(&self, key: &Key) -> Option<&Desired> {
        self.objects
            .iter()
            .find(|(known, _)| known == key)
            .map(|(_, desired)| desired)
    }

    /// Names of the desired model tables, raw landing tables excluded.
    fn model_table_names(&self) -> Vec<&str> {
        self.objects
            .iter()
            .filter(|(key, desired)| {
                matches!(desired, Desired::Table) && !key.name.starts_with(RAW_TABLE_NAME_PREFIX)
            })
            .map(|(key, _)| key.name.as_str())
            .collect()
    }
}

fn push_unique<T: PartialEq>(items: &mut Vec<T>, item: T) {
    if !items.contains(&item) {
        items.push(item);
    }
}

fn unique<T: PartialEq>(items: Vec<T>) -> Vec<T> {
    let mut kept: Vec<T> = Vec::new();
    for item in items {
        push_unique(&mut kept, item);
    }
    kept
}

/// `json.dumps(payload, indent=2) + "\n"` of the plan.
pub fn render_plan_json(plan: &Plan, adapter_name: &str) -> String {
    let text = |value: &str| JsonValue::String(value.to_owned());
    let optional = |value: &Option<String>| value.as_deref().map_or(JsonValue::Null, text);
    let boolean = JsonValue::Bool;
    let key = |key: &Key| {
        object(vec![
            ("database", optional(&key.database)),
            ("object_type", text(&key.object_type)),
            ("name", text(&key.name)),
        ])
    };
    let optional_key = |value: &Option<Key>| value.as_ref().map_or(JsonValue::Null, key);
    let array = |items: Vec<JsonValue>| JsonValue::Array(items);
    let payload: JsonValue = object(vec![
        ("mode", text(VIRTUAL_ENVIRONMENTS_MODE_LABEL)),
        ("adapter", text(adapter_name)),
        ("deployment_id", optional(&plan.deployment_id)),
        (
            "object_changes",
            array(
                plan.object_changes
                    .iter()
                    .map(|change| {
                        object(vec![
                            ("key", key(&change.key)),
                            ("change_type", text(&change.change_type)),
                            ("force_full_refresh", boolean(change.force_full_refresh)),
                            ("forced_start_time", optional(&change.forced_start_time)),
                            ("schema_change_kind", optional(&change.schema_change_kind)),
                            ("seed_compatibility", optional(&change.seed_compatibility)),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "steps",
            array(
                plan.steps
                    .iter()
                    .map(|step| {
                        object(vec![
                            ("step_id", text(&step.step_id)),
                            ("phase", text(&step.phase)),
                            ("action", text(&step.action)),
                            ("root_key", key(&step.root_key)),
                            ("target_key", optional_key(&step.target_key)),
                            ("physical_name", optional(&step.physical_name)),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "rebuild_subtrees",
            array(
                plan.rebuild_subtrees
                    .iter()
                    .map(|subtree| {
                        object(vec![
                            ("root_key", key(&subtree.root_key)),
                            ("upstream_boundary_key", key(&subtree.upstream_boundary_key)),
                            (
                                "affected_keys",
                                array(subtree.affected_keys.iter().map(key).collect()),
                            ),
                            ("strategy", text(&subtree.strategy)),
                            ("replay_required", boolean(subtree.replay_required)),
                            ("execution_mode", text(&subtree.execution_mode)),
                            ("forced_full_refresh", boolean(subtree.forced_full_refresh)),
                            ("forced_start_time", optional(&subtree.forced_start_time)),
                            (
                                "requested_start_time",
                                optional(&subtree.requested_start_time),
                            ),
                            (
                                "configured_backfill_mode",
                                optional(&subtree.configured_backfill_mode),
                            ),
                            (
                                "execution_lookback_seconds",
                                subtree
                                    .execution_lookback_seconds
                                    .map_or(JsonValue::Null, |seconds| {
                                        JsonValue::Integer(JsonInteger::from(seconds))
                                    }),
                            ),
                            (
                                "history_preserving_bounded_supported",
                                boolean(subtree.history_preserving_bounded_supported),
                            ),
                            (
                                "resolved_bounded_replay_fallback",
                                optional(&subtree.resolved_bounded_replay_fallback),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "prepared_shadow_objects",
            array(
                plan.prepared_shadow_objects
                    .iter()
                    .map(|prepared| {
                        object(vec![
                            ("logical_key", key(&prepared.logical_key)),
                            ("physical_name", text(&prepared.physical_name)),
                            ("logical_model_name", text(&prepared.logical_model_name)),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "warnings",
            array(
                plan.warnings
                    .iter()
                    .map(|warning| {
                        object(vec![
                            ("warning_code", text(&warning.warning_code)),
                            ("root_key", key(&warning.root_key)),
                            ("target_key", optional_key(&warning.target_key)),
                            ("message", text(&warning.message)),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "sql_diffs",
            array(
                plan.sql_diffs
                    .iter()
                    .map(|sql_diff| {
                        object(vec![
                            ("key", key(&sql_diff.key)),
                            ("object_type", text(&sql_diff.object_type)),
                            ("name", text(&sql_diff.name)),
                            (
                                "diff_lines",
                                array(sql_diff.diff_lines.iter().map(|line| text(line)).collect()),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]);
    let mut rendered: String = dumps(
        &payload,
        &JsonDialect::Stdlib(StdlibJsonOptions::new(Some(2))),
    )
    .expect("a plan payload holds only text, booleans, integers and null");
    rendered.push('\n');
    rendered
}

pub(crate) fn object(fields: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        fields
            .into_iter()
            .map(|(name, value)| (name.to_owned().into(), value))
            .collect(),
    )
}

/// The human plan: header, subtrees, then compact or verbose change sections and warnings.
pub fn render_plan_text(
    plan: &Plan,
    desired: &DesiredObjects,
    database: &str,
    verbose: bool,
    style: CliStyle,
) -> String {
    let renderer = Renderer {
        plan,
        desired,
        style,
    };
    let mut lines: Vec<String> = vec![
        style.title("Plan Ready"),
        style.label_value("Database", database),
        style.label_value(
            "Subtrees to rebuild",
            &plan.rebuild_subtrees.len().to_string(),
        ),
        style.label_value("Planned steps", &plan.steps.len().to_string()),
        String::new(),
    ];
    renderer.subtrees(&mut lines);
    if verbose {
        renderer.changed_objects(&mut lines);
        renderer.sql_diffs(&mut lines);
    } else {
        renderer.compact_summary(&mut lines);
        renderer.compact_diffs(&mut lines);
    }
    if verbose && !plan.rebuild_subtrees.is_empty() {
        renderer.rollout_objects(&mut lines);
    }
    if verbose && !plan.steps.is_empty() {
        lines.push(style.section("Workflow"));
        renderer.workflow_summary(&mut lines);
        lines.push(String::new());
    }
    lines.push(style.section("Warnings"));
    if plan.warnings.is_empty() {
        lines.push("- none".to_owned());
    }
    for warning in &plan.warnings {
        lines.push(format!(
            "- {}: {}",
            style.warning(&warning.root_key.name),
            style.warning(&warning.message)
        ));
    }
    lines.join("\n")
}

struct Renderer<'a> {
    plan: &'a Plan,
    desired: &'a DesiredObjects,
    style: CliStyle,
}

fn is_live_type(key: &Key) -> bool {
    key.object_type == TABLE || key.object_type == VIEW
}

fn is_new_change(change: &Change) -> bool {
    (change.change_type == CREATE || change.change_type == REPLACE)
        && change.schema_change_kind.is_none()
}

impl Renderer<'_> {
    fn subtrees(&self, lines: &mut Vec<String>) {
        if self.plan.rebuild_subtrees.is_empty() {
            return;
        }
        lines.push(self.style.section("Subtrees"));
        for (index, subtree) in self.plan.rebuild_subtrees.iter().enumerate() {
            if index > 0 {
                lines.push(String::new());
            }
            lines.push(self.style.subsection(&format!("Subtree {}", index + 1)));
            lines.push(if subtree.replay_required {
                format!("[replay start] {}", subtree.upstream_boundary_key.name)
            } else {
                format!("[deploy] {}", subtree.root_key.name)
            });
            let live_targets: Vec<&str> = self.live_target_names(subtree);
            if live_targets.is_empty() {
                lines.push(format!("└── [live target] {}", subtree.root_key.name));
                continue;
            }
            for (position, name) in live_targets.iter().enumerate() {
                let branch = if position + 1 == live_targets.len() {
                    "└──"
                } else {
                    "├──"
                };
                lines.push(format!("{branch} [live target] {name}"));
            }
        }
        lines.push(String::new());
    }

    /// The live tables and views a subtree publishes, by name.
    fn live_target_names<'s>(&'s self, subtree: &'s Subtree) -> Vec<&'s str> {
        let mut live: Vec<&str> = subtree
            .affected_keys
            .iter()
            .filter(|key| is_live_type(key) && !key.name.starts_with(RAW_TABLE_NAME_PREFIX))
            .map(|key| key.name.as_str())
            .collect();
        if !live.is_empty() {
            live.sort_unstable();
            return live;
        }
        match self.desired.get(&subtree.root_key) {
            Some(Desired::MaterializedView { target_table_name })
                if self
                    .desired
                    .model_table_names()
                    .contains(&target_table_name.as_str()) =>
            {
                vec![target_table_name.as_str()]
            }
            Some(Desired::Table | Desired::View)
                if !subtree.root_key.name.starts_with(RAW_TABLE_NAME_PREFIX) =>
            {
                vec![subtree.root_key.name.as_str()]
            }
            _ => Vec::new(),
        }
    }

    fn subtree_for_key(&self, key: &Key) -> Option<&Subtree> {
        self.plan
            .rebuild_subtrees
            .iter()
            .find(|subtree| *key == subtree.root_key || subtree.affected_keys.contains(key))
    }

    fn target_is_new(&self, target_name: &str, subtree: &Subtree) -> bool {
        self.plan.object_changes.iter().any(|change| {
            subtree.affected_keys.contains(&change.key)
                && is_live_type(&change.key)
                && change.key.name == target_name
                && is_new_change(change)
        })
    }

    fn compact_summary(&self, lines: &mut Vec<String>) {
        let mut new_targets: Vec<&str> = Vec::new();
        for subtree in &self.plan.rebuild_subtrees {
            let subtree_new: Vec<&str> = self
                .plan
                .object_changes
                .iter()
                .filter(|change| {
                    subtree.affected_keys.contains(&change.key)
                        && is_live_type(&change.key)
                        && is_new_change(change)
                })
                .map(|change| change.key.name.as_str())
                .collect();
            if !subtree_new.is_empty() {
                for name in subtree_new {
                    push_unique(&mut new_targets, name);
                }
                continue;
            }
            for name in self.live_target_names(subtree) {
                if self.target_is_new(name, subtree) {
                    push_unique(&mut new_targets, name);
                }
            }
        }
        if !new_targets.is_empty() {
            lines.push(self.style.section("New targets"));
            lines.extend(new_targets.iter().map(|name| format!("- {name}")));
            lines.push(String::new());
        }

        let mut summaries: Vec<(&str, Vec<String>)> = Vec::new();
        for subtree in &self.plan.rebuild_subtrees {
            for name in self.live_target_names(subtree) {
                if self.target_is_new(name, subtree) {
                    continue;
                }
                let details: Vec<String> = self.changed_target_details(name, subtree);
                if details.is_empty() {
                    continue;
                }
                match summaries.iter_mut().find(|(known, _)| *known == name) {
                    Some((_, known)) => known.extend(details),
                    None => summaries.push((name, details)),
                }
            }
        }
        if summaries.is_empty() {
            return;
        }
        lines.push(self.style.section("Changes detected"));
        for (index, (name, details)) in summaries.into_iter().enumerate() {
            if index > 0 {
                lines.push(String::new());
            }
            lines.push(name.to_owned());
            lines.extend(unique(details).iter().map(|line| format!("- {line}")));
        }
        lines.push(String::new());
    }

    fn changed_target_details(&self, target_name: &str, subtree: &Subtree) -> Vec<String> {
        let mut details: Vec<String> = Vec::new();
        for change in &self.plan.object_changes {
            if !subtree.affected_keys.contains(&change.key) || change.change_type == NO_OP {
                continue;
            }
            match self.desired.get(&change.key) {
                Some(Desired::MaterializedView { target_table_name })
                    if target_table_name == target_name && change.schema_change_kind.is_none() =>
                {
                    details.push("transform query changed".to_owned());
                    continue;
                }
                Some(Desired::View) => {
                    details.push("view query changed".to_owned());
                    continue;
                }
                _ => {}
            }
            if change.key.object_type != TABLE || change.key.name != target_name {
                continue;
            }
            let plan_line = || format!("plan: {}", describe_rebuild_behavior(subtree));
            if change.force_full_refresh {
                details.push("full refresh requested".to_owned());
                details.push(plan_line());
                continue;
            }
            let requested: Option<&String> = subtree
                .requested_start_time
                .as_ref()
                .filter(|requested| !requested.is_empty())
                .or(change.forced_start_time.as_ref());
            if let Some(requested) = requested {
                details.push(format!("start time requested: {requested}"));
                details.extend(unsupported_replay_details(subtree));
                details.push(plan_line());
                continue;
            }
            if change.schema_change_kind.is_some() {
                details.push("table schema changed".to_owned());
                if subtree.configured_backfill_mode.is_some() {
                    details.push(format!(
                        "replay on change: {}",
                        format_backfill_policy(subtree)
                    ));
                }
                details.extend(unsupported_replay_details(subtree));
                details.push(plan_line());
                continue;
            }
            if change.change_type == REPLACE || change.change_type == REBUILD {
                details.push("table definition changed".to_owned());
            }
        }
        unique(details)
    }

    fn compact_diffs(&self, lines: &mut Vec<String>) {
        if self.plan.sql_diffs.is_empty() {
            return;
        }
        lines.push(self.style.section("Diffs"));
        let mut names: Vec<&str> = Vec::new();
        for sql_diff in &self.plan.sql_diffs {
            let live: Vec<&str> = self
                .subtree_for_key(&sql_diff.key)
                .map(|subtree| self.live_target_names(subtree))
                .unwrap_or_default();
            if live.is_empty() {
                push_unique(&mut names, sql_diff.name.as_str());
            }
            for name in live {
                push_unique(&mut names, name);
            }
        }
        lines.extend(names.iter().map(|name| format!("- {name}")));
        lines.push("Run `sqt plan --verbose` to show full diffs".to_owned());
        lines.push(String::new());
    }

    fn changed_objects(&self, lines: &mut Vec<String>) {
        let model_tables: Vec<&str> = self.desired.model_table_names();
        let mut entries: Vec<(String, &str)> = Vec::new();
        for change in &self.plan.object_changes {
            if change.change_type == NO_OP {
                continue;
            }
            let entry: (String, &str) = match self.desired.get(&change.key) {
                Some(Desired::MaterializedView { target_table_name })
                    if model_tables.contains(&target_table_name.as_str()) =>
                {
                    ("transform".to_owned(), change.key.name.as_str())
                }
                _ => (
                    humanize_object_type(&change.key.object_type),
                    change.key.name.as_str(),
                ),
            };
            push_unique(&mut entries, entry);
        }
        if entries.is_empty() {
            return;
        }
        lines.push(self.style.section("Changed objects"));
        lines.extend(
            entries
                .iter()
                .map(|(kind, name)| format!("- {kind}: {name}")),
        );
        lines.push(String::new());
    }

    fn sql_diffs(&self, lines: &mut Vec<String>) {
        if self.plan.sql_diffs.is_empty() {
            return;
        }
        lines.push(self.style.section("SQL diffs"));
        for sql_diff in &self.plan.sql_diffs {
            lines.push(String::new());
            lines.push(self.style.subsection(&format!(
                "{}: {}",
                python_title(&sql_diff.object_type),
                self.style.object_name(&sql_diff.name)
            )));
            self.sql_diff_context(sql_diff, lines);
            lines.extend(
                sql_diff
                    .diff_lines
                    .iter()
                    .map(|line| self.style.diff_line(line)),
            );
        }
        lines.push(String::new());
    }

    fn sql_diff_context(&self, sql_diff: &SqlDiff, lines: &mut Vec<String>) {
        let Some(subtree) = self.subtree_for_key(&sql_diff.key) else {
            return;
        };
        let live: Vec<&str> = self.live_target_names(subtree);
        if !live.is_empty() {
            let label: &str =
                if sql_diff.object_type == TABLE && live.contains(&sql_diff.name.as_str()) {
                    "Live target"
                } else {
                    "Affects live target"
                };
            lines.push(self.style.label_value(label, &live.join(", ")));
        }
        let schema_changes: Vec<&Change> = self
            .plan
            .object_changes
            .iter()
            .filter(|change| {
                subtree.affected_keys.contains(&change.key) && change.schema_change_kind.is_some()
            })
            .collect();
        let schema_change: Option<&Change> = schema_changes
            .iter()
            .find(|change| change.key.object_type == TABLE)
            .or(schema_changes.first())
            .copied();
        if sql_diff.object_type == TABLE && schema_change.is_some() {
            if subtree.configured_backfill_mode.is_some() {
                lines.push(
                    self.style
                        .label_value("Replay on change", &format_backfill_policy(subtree)),
                );
            }
            lines.push(
                self.style
                    .label_value("Plan", &describe_rebuild_behavior(subtree)),
            );
        }
    }

    fn rollout_objects(&self, lines: &mut Vec<String>) {
        lines.push(self.style.section("Staged rollout objects"));
        let mut entries: Vec<(&str, &str)> = Vec::new();
        for subtree in &self.plan.rebuild_subtrees {
            if subtree.replay_required {
                push_unique(
                    &mut entries,
                    ("replay source", subtree.upstream_boundary_key.name.as_str()),
                );
            }
            match self.desired.get(&subtree.root_key) {
                Some(Desired::MaterializedView { .. }) => {
                    push_unique(&mut entries, ("transform", subtree.root_key.name.as_str()));
                }
                Some(Desired::View) => {
                    push_unique(&mut entries, ("view", subtree.root_key.name.as_str()));
                }
                _ => {}
            }
            for name in self.live_target_names(subtree) {
                push_unique(&mut entries, ("live target", name));
            }
        }
        if entries.is_empty() {
            lines.push("- none".to_owned());
        }
        lines.extend(
            entries
                .iter()
                .map(|(kind, name)| format!("- {kind}: {name}")),
        );
        lines.push(String::new());
    }

    fn workflow_summary(&self, lines: &mut Vec<String>) {
        for subtree in &self.plan.rebuild_subtrees {
            let live: Vec<&str> = self.live_target_names(subtree);
            let target: &str = live.first().copied().unwrap_or(&subtree.root_key.name);
            lines.push(format!(
                "- prepare staged objects for subtree rooted at {target}"
            ));
            if subtree.replay_required {
                lines.push(format!(
                    "- backfill from {}",
                    subtree.upstream_boundary_key.name
                ));
            }
            if live.is_empty() {
                lines.push(format!("- audit subtree rooted at {target}"));
                lines.push(format!("- publish subtree rooted at {target}"));
            } else {
                lines.push(format!("- audit staged {}", live.join(", ")));
                lines.push(format!("- publish {}", live.join(", ")));
            }
        }
    }
}

fn format_backfill_policy(subtree: &Subtree) -> String {
    if subtree.configured_backfill_mode.as_deref() == Some("full") {
        return "full".to_owned();
    }
    match subtree.execution_lookback_seconds {
        None => "bounded".to_owned(),
        Some(seconds) => format!("bounded-{}", format_duration_seconds(seconds)),
    }
}

/// How the subtree rebuild replays history, in the operator's terms.
pub fn describe_rebuild_behavior(subtree: &Subtree) -> String {
    let fallback: Option<&str> = subtree.resolved_bounded_replay_fallback.as_deref();
    if subtree.execution_mode == FULL_REBUILD {
        return if subtree.forced_full_refresh {
            "full refresh requested; replay all history for this subtree"
        } else if fallback == Some(FALLBACK_FULL) {
            "full refresh will be used instead of bounded replay"
        } else {
            "refresh all history for this subtree"
        }
        .to_owned();
    }
    if let Some(start_time) = &subtree.forced_start_time {
        return format!("bounded replay with history from {start_time} onward");
    }
    if fallback == Some(FALLBACK_BOUNDED_WITHOUT_HISTORY) {
        return "bounded replay without history; older rows will not be preserved".to_owned();
    }
    let lookback: Option<String> = subtree
        .execution_lookback_seconds
        .map(format_duration_seconds);
    if subtree.execution_mode == SEEDED_BOUNDED_REBUILD {
        return match lookback {
            None => {
                "bounded replay with history will keep older active rows and replay the recent tail"
                    .to_owned()
            }
            Some(lookback) => format!(
                "bounded replay with history will keep older active rows and replay the last \
                 {lookback}"
            ),
        };
    }
    match lookback {
        None => "bounded replay without history will replay only the recent tail in the new schema"
            .to_owned(),
        Some(lookback) => format!(
            "bounded replay without history will replay only the last {lookback} in the new schema"
        ),
    }
}

fn unsupported_replay_details(subtree: &Subtree) -> Vec<String> {
    if subtree.history_preserving_bounded_supported {
        return Vec::new();
    }
    let fallback: &str = match subtree.resolved_bounded_replay_fallback.as_deref() {
        Some(fallback @ (FALLBACK_FULL | FALLBACK_BOUNDED_WITHOUT_HISTORY)) => fallback,
        _ => return Vec::new(),
    };
    vec![
        "bounded replay cannot preserve prior history for this target".to_owned(),
        format!("bounded replay fallback: {fallback}"),
    ]
}

/// Whole days, hours or minutes when `seconds` divides evenly, otherwise seconds.
pub fn format_duration_seconds(seconds: i64) -> String {
    for (unit, suffix) in [(24 * 60 * 60, "d"), (60 * 60, "h"), (60, "m")] {
        if seconds.rem_euclid(unit) == 0 {
            return format!("{}{suffix}", seconds.div_euclid(unit));
        }
    }
    format!("{seconds}s")
}

fn humanize_object_type(value: &str) -> String {
    match value {
        "table" => "table".to_owned(),
        "materialized_view" => "materialized view".to_owned(),
        "kafka_table" => "kafka table".to_owned(),
        "view" => "view".to_owned(),
        other => other.replace('_', " "),
    }
}

/// Python's `str.title()`: upper-case each character that follows an uncased one.
fn python_title(text: &str) -> String {
    let mut titled: String = String::with_capacity(text.len());
    let mut previous_cased: bool = false;
    for character in text.chars() {
        if previous_cased {
            titled.extend(character.to_lowercase());
        } else {
            titled.extend(character.to_uppercase());
        }
        previous_cased = character.is_lowercase() || character.is_uppercase();
    }
    titled
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durations_use_the_largest_even_unit() {
        let cases = [
            (0, "0d"),
            (86_400, "1d"),
            (7_200, "2h"),
            (5_400, "90m"),
            (61, "61s"),
        ];
        for (seconds, expected) in cases {
            assert_eq!(format_duration_seconds(seconds), expected);
        }
    }

    #[test]
    fn title_matches_python_for_display_types() {
        assert_eq!(python_title("kafka table"), "Kafka Table");
        assert_eq!(python_title("materialized view"), "Materialized View");
        assert_eq!(python_title("x2y_zAB"), "X2Y_Zab");
    }
}
