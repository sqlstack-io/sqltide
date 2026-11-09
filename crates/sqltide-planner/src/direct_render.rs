//! Text and JSON rendering of one direct-mode plan for `sqt plan` and `sqt build`.

use sqltide_json::{JsonDialect, JsonInteger, JsonValue, StdlibJsonOptions, dumps};

use crate::cli_style::CliStyle;
use crate::direct::split_lines;
use crate::plan_render::{Key, Warning, object};

const DIRECT_MODE_LABEL: &str = "direct";
const DEFAULT_DIFF_LINE_LIMIT: usize = 12;

/// One logical resource identity, compared on its type and name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LogicalKey {
    pub resource_type: String,
    pub name: String,
}

pub struct Prerequisite {
    pub key: LogicalKey,
    pub relation_names: Vec<String>,
    pub present: bool,
    pub framework_managed: bool,
}

pub struct SqlChange {
    pub status: String,
    pub current_sql: String,
    pub current_hash: String,
    pub previous_sql: Option<String>,
    pub previous_hash: Option<String>,
    pub unified_diff: Option<String>,
    pub warning: Option<String>,
}

pub struct Entry {
    pub model_key: LogicalKey,
    pub reason: String,
    pub relation_names: Vec<String>,
    pub resource_kinds: Vec<String>,
    pub driving_input_key: Option<LogicalKey>,
    pub is_replay_root: bool,
    pub sql_change: Option<SqlChange>,
}

pub struct ReplayColumns {
    pub partition: String,
    pub offset: String,
    pub timestamp: String,
    pub landed_at: String,
    pub cursor: String,
}

pub struct ReplayRoot {
    pub model_key: LogicalKey,
    pub driving_input_key: LogicalKey,
    pub driving_input_relation_name: String,
    pub driving_input_replay_columns: ReplayColumns,
    pub replay_boundary_mode: String,
    pub propagated_model_keys: Vec<LogicalKey>,
    pub has_aggregate_semantics: bool,
    pub replay_settings: Vec<(String, String)>,
}

pub struct RelationOperation {
    pub relation_name: String,
    pub action: String,
    pub model_key: LogicalKey,
    pub resource_kind: String,
}

pub struct SourceDriftSelection {
    pub source_key: LogicalKey,
    pub pipeline_name: String,
    pub reader_pipeline_names: Vec<String>,
    pub drifted_setting_names: Vec<String>,
    pub model_keys: Vec<LogicalKey>,
}

/// One drifted managed source recreated with its landing table kept.
pub struct SourceRecreation {
    pub relation_name: String,
    pub landing_view_name: String,
    pub landing_relation_name: String,
    pub consumer_group: String,
    pub drifted_setting_names: Vec<String>,
}

pub struct RowErrorPolicyChange {
    pub model_key: LogicalKey,
    pub view_name: String,
    pub dead_letter_view_name: String,
    pub desired_policy: String,
    pub landing_view_names: Vec<String>,
    pub landing_repair_only: bool,
}

pub struct DirectPlan {
    pub database: String,
    pub user_scope: Vec<LogicalKey>,
    pub execution_scope: Vec<LogicalKey>,
    pub prerequisite_scope: Vec<Prerequisite>,
    pub entries: Vec<Entry>,
    pub replay_roots: Vec<ReplayRoot>,
    pub teardown_operations: Vec<RelationOperation>,
    pub creation_operations: Vec<RelationOperation>,
    pub selection_mode: String,
    pub prerequisite_execution_scope: Vec<LogicalKey>,
    pub warnings: Vec<Warning>,
    pub effective_start_time: Option<String>,
    pub source_drift_selections: Vec<SourceDriftSelection>,
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

/// The plan line for one in-place row-error policy change, landing-view repairs apart.
pub fn render_row_error_policy_line(change: &RowErrorPolicyChange) -> String {
    if change.landing_repair_only {
        return format!(
            "repair landing view  {}: recreate {}, no replay",
            change.model_key.name,
            change.landing_view_names.join(", ")
        );
    }
    format!(
        "view-only  {}: on_row_error -> {}, recreate {}, no replay",
        change.model_key.name, change.desired_policy, change.view_name
    )
}

/// One operator-facing line describing a drifted managed source recreation.
pub fn render_source_recreation_notice(recreation: &SourceRecreation) -> String {
    format!(
        "{} and {} are recreated with the declared settings ({}); {} and its rows are kept. \
         Consumption resumes from the committed offsets of consumer group '{}', or follows the \
         Kafka engine's offset reset policy on a topic where the group has none.",
        recreation.relation_name,
        recreation.landing_view_name,
        recreation.drifted_setting_names.join(", "),
        recreation.landing_relation_name,
        recreation.consumer_group
    )
}

/// Which owning pipeline a changed selection rebuilds for one drifted source.
fn render_source_drift_selection(selection: &SourceDriftSelection) -> String {
    let other_readers: Vec<&str> = selection
        .reader_pipeline_names
        .iter()
        .filter(|name| **name != selection.pipeline_name)
        .map(String::as_str)
        .collect();
    let kept_note: String = if other_readers.is_empty() {
        String::new()
    } else {
        format!(
            "; {} keep reading the preserved landing table",
            other_readers.join(", ")
        )
    };
    format!(
        "{} settings drifted ({}); --changed rebuilds pipeline:{} to recreate it{kept_note}",
        selection.source_key.name,
        selection.drifted_setting_names.join(", "),
        selection.pipeline_name
    )
}

/// The operator-facing text of one direct plan, without a trailing newline.
pub fn render_direct_plan_text(
    plan: &DirectPlan,
    adapter_name: &str,
    verbose: bool,
    source_recreations: &[SourceRecreation],
    row_error_policy_changes: &[RowErrorPolicyChange],
    style: CliStyle,
) -> String {
    let mut lines: Vec<String> = vec![
        style.title(&format!("Direct plan  {}", plan.database)),
        String::new(),
    ];
    if verbose {
        lines.push(style.label_value("Adapter", adapter_name));
        lines.push(String::new());
    }
    push_rebuild_paths(&mut lines, plan, style);
    lines.push(style.section("Model decisions"));
    lines.push(String::new());
    for entry in &plan.entries {
        lines.push(style.object_name(&entry.model_key.name));
        if let Some(sql_change) = &entry.sql_change {
            push_sql_change(&mut lines, sql_change, verbose, style);
        }
        lines.push(format!("  {}  {}", style.label("reason"), entry.reason));
        lines.push(format!(
            "  {}  {}",
            style.label("rebuild"),
            entry.relation_names.join(", ")
        ));
        lines.push(String::new());
    }
    for selection in &plan.source_drift_selections {
        lines.push(format!(
            "{}  {}",
            style.label("source"),
            render_source_drift_selection(selection)
        ));
    }
    for recreation in source_recreations {
        lines.push(format!(
            "{}  {}",
            style.label("source"),
            render_source_recreation_notice(recreation)
        ));
    }
    if let Some(start_time) = &plan.effective_start_time {
        lines.push(style.label_value("Start time", start_time));
    }
    for warning in &plan.warnings {
        lines.push(style.warning(&format!("warning  {}", warning.message)));
    }
    lines.extend(
        row_error_policy_changes
            .iter()
            .map(render_row_error_policy_line),
    );
    let policy_count: usize = row_error_policy_changes.len();
    let policy_suffix: String = if policy_count == 0 {
        String::new()
    } else {
        format!(
            ", {policy_count} view-only row-error policy change{} (no replay)",
            plural(policy_count)
        )
    };
    let model_count: usize = plan.execution_scope.len();
    let relation_count: usize = plan.creation_operations.len();
    let replay_count: usize = plan.replay_roots.len();
    lines.push(format!(
        "{model_count} model{} selected, {relation_count} relation{} rebuilt, \
         {replay_count} replay root{}{policy_suffix}",
        plural(model_count),
        plural(relation_count),
        plural(replay_count)
    ));
    lines.join("\n")
}

/// Replay roots grouped by driving source and boundary mode, then models outside every root.
fn push_rebuild_paths(lines: &mut Vec<String>, plan: &DirectPlan, style: CliStyle) {
    lines.push(style.section("Rebuild paths"));
    lines.push(String::new());
    let mut roots_by_source: Vec<((&str, &str), Vec<&ReplayRoot>)> = Vec::new();
    for root in &plan.replay_roots {
        let identity = (
            root.driving_input_relation_name.as_str(),
            root.replay_boundary_mode.as_str(),
        );
        match roots_by_source
            .iter_mut()
            .find(|(known, _)| *known == identity)
        {
            Some((_, roots)) => roots.push(root),
            None => roots_by_source.push((identity, vec![root])),
        }
    }
    let mut covered: Vec<&LogicalKey> = Vec::new();
    for (source_index, ((relation_name, mode), roots)) in roots_by_source.iter().enumerate() {
        if source_index > 0 {
            lines.push(String::new());
        }
        lines.push(format!("[replay source] {relation_name} [{mode}]"));
        for (root_index, root) in roots.iter().enumerate() {
            let root_is_last: bool = root_index == roots.len() - 1;
            let root_branch: &str = if root_is_last {
                "└──"
            } else {
                "├──"
            };
            lines.push(format!(
                "{root_branch} [replay root] {}",
                root.model_key.name
            ));
            let propagated: Vec<&LogicalKey> = root
                .propagated_model_keys
                .iter()
                .filter(|key| **key != root.model_key)
                .collect();
            let child_prefix: &str = if root_is_last { "    " } else { "│   " };
            for (child_index, key) in propagated.iter().enumerate() {
                let child_branch: &str = if child_index == propagated.len() - 1 {
                    "└──"
                } else {
                    "├──"
                };
                lines.push(format!(
                    "{child_prefix}{child_branch} [rebuild] {}",
                    key.name
                ));
            }
            covered.extend(&root.propagated_model_keys);
        }
    }
    for entry in &plan.entries {
        if !covered.contains(&&entry.model_key) {
            lines.push(format!("[rebuild] {}", entry.model_key.name));
        }
    }
    lines.push(String::new());
}

/// The SQL baseline status, its warning and a diff capped unless `verbose`.
fn push_sql_change(
    lines: &mut Vec<String>,
    sql_change: &SqlChange,
    verbose: bool,
    style: CliStyle,
) {
    let label: &str = match sql_change.status.as_str() {
        "first_baseline" => "first SQL baseline",
        "query_changed" => "SQL changed",
        "no_query_change" => "no SQL change",
        "baseline_unavailable" => "SQL baseline unavailable",
        other => other,
    };
    lines.push(format!("  {label}"));
    if let Some(warning) = &sql_change.warning {
        lines.push(format!("    {}", style.warning(warning)));
    }
    if let Some(unified_diff) = &sql_change.unified_diff {
        let diff_lines: Vec<&str> = split_lines(unified_diff, false);
        let visible: usize = if verbose {
            diff_lines.len()
        } else {
            diff_lines.len().min(DEFAULT_DIFF_LINE_LIMIT)
        };
        lines.extend(
            diff_lines[..visible]
                .iter()
                .map(|line| format!("    {}", style.diff_line(line))),
        );
        if visible < diff_lines.len() {
            lines.push(style.muted("    ... use --verbose for the complete diff"));
        }
    }
}

/// `json.dumps(payload, indent=2) + "\n"` of one direct plan.
pub fn render_direct_plan_json(
    plan: &DirectPlan,
    adapter_name: &str,
    source_recreations: &[SourceRecreation],
    row_error_policy_changes: &[RowErrorPolicyChange],
) -> String {
    let text = |value: &str| JsonValue::String(value.to_owned());
    let optional = |value: &Option<String>| value.as_deref().map_or(JsonValue::Null, text);
    let texts =
        |values: &[String]| JsonValue::Array(values.iter().map(|value| text(value)).collect());
    let logical = |key: &LogicalKey| {
        object(vec![
            ("resource_type", text(&key.resource_type)),
            ("name", text(&key.name)),
        ])
    };
    let logicals = |keys: &[LogicalKey]| JsonValue::Array(keys.iter().map(logical).collect());
    let object_key = |key: &Key| {
        object(vec![
            ("database", optional(&key.database)),
            ("object_type", text(&key.object_type)),
            ("name", text(&key.name)),
        ])
    };
    let operations = |operations: &[RelationOperation]| {
        JsonValue::Array(
            operations
                .iter()
                .map(|operation| {
                    object(vec![
                        ("relation_name", text(&operation.relation_name)),
                        ("action", text(&operation.action)),
                        ("model_key", logical(&operation.model_key)),
                        ("resource_kind", text(&operation.resource_kind)),
                    ])
                })
                .collect(),
        )
    };
    let mut payload: Vec<(&str, JsonValue)> = vec![
        ("mode", text(DIRECT_MODE_LABEL)),
        ("adapter", text(adapter_name)),
        ("database", text(&plan.database)),
        ("start_time", optional(&plan.effective_start_time)),
        ("selection_mode", text(&plan.selection_mode)),
        ("user_scope", logicals(&plan.user_scope)),
        ("execution_scope", logicals(&plan.execution_scope)),
        (
            "prerequisite_execution_scope",
            logicals(&plan.prerequisite_execution_scope),
        ),
        (
            "prerequisite_scope",
            JsonValue::Array(
                plan.prerequisite_scope
                    .iter()
                    .map(|prerequisite| {
                        object(vec![
                            ("key", logical(&prerequisite.key)),
                            ("relation_names", texts(&prerequisite.relation_names)),
                            ("present", JsonValue::Bool(prerequisite.present)),
                            (
                                "framework_managed",
                                JsonValue::Bool(prerequisite.framework_managed),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "entries",
            JsonValue::Array(
                plan.entries
                    .iter()
                    .map(|entry| {
                        object(vec![
                            ("model_key", logical(&entry.model_key)),
                            ("reason", text(&entry.reason)),
                            ("relation_names", texts(&entry.relation_names)),
                            ("resource_kinds", texts(&entry.resource_kinds)),
                            (
                                "driving_input_key",
                                entry
                                    .driving_input_key
                                    .as_ref()
                                    .map_or(JsonValue::Null, logical),
                            ),
                            ("is_replay_root", JsonValue::Bool(entry.is_replay_root)),
                            (
                                "sql_change",
                                entry.sql_change.as_ref().map_or(JsonValue::Null, |change| {
                                    object(vec![
                                        ("status", text(&change.status)),
                                        ("current_sql", text(&change.current_sql)),
                                        ("current_hash", text(&change.current_hash)),
                                        ("previous_sql", optional(&change.previous_sql)),
                                        ("previous_hash", optional(&change.previous_hash)),
                                        ("unified_diff", optional(&change.unified_diff)),
                                        ("warning", optional(&change.warning)),
                                    ])
                                }),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "replay_roots",
            JsonValue::Array(
                plan.replay_roots
                    .iter()
                    .map(|root| {
                        let columns: &ReplayColumns = &root.driving_input_replay_columns;
                        object(vec![
                            ("model_key", logical(&root.model_key)),
                            ("driving_input_key", logical(&root.driving_input_key)),
                            (
                                "driving_input_relation_name",
                                text(&root.driving_input_relation_name),
                            ),
                            (
                                "driving_input_replay_columns",
                                object(vec![
                                    ("partition", text(&columns.partition)),
                                    ("offset", text(&columns.offset)),
                                    ("timestamp", text(&columns.timestamp)),
                                    ("landed_at", text(&columns.landed_at)),
                                    ("cursor", text(&columns.cursor)),
                                ]),
                            ),
                            ("replay_boundary_mode", text(&root.replay_boundary_mode)),
                            (
                                "propagated_model_keys",
                                logicals(&root.propagated_model_keys),
                            ),
                            (
                                "has_aggregate_semantics",
                                JsonValue::Bool(root.has_aggregate_semantics),
                            ),
                            ("settings", settings_object(&root.replay_settings)),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("teardown", operations(&plan.teardown_operations)),
        ("creation", operations(&plan.creation_operations)),
        (
            "source_drift_selections",
            JsonValue::Array(
                plan.source_drift_selections
                    .iter()
                    .map(|selection| {
                        object(vec![
                            ("source_name", text(&selection.source_key.name)),
                            ("pipeline_name", text(&selection.pipeline_name)),
                            (
                                "reader_pipeline_names",
                                texts(&selection.reader_pipeline_names),
                            ),
                            ("drifted_settings", texts(&selection.drifted_setting_names)),
                            ("model_keys", logicals(&selection.model_keys)),
                            ("message", text(&render_source_drift_selection(selection))),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "source_recreations",
            JsonValue::Array(
                source_recreations
                    .iter()
                    .map(|recreation| {
                        object(vec![
                            ("relation_name", text(&recreation.relation_name)),
                            ("landing_view_name", text(&recreation.landing_view_name)),
                            (
                                "landing_relation_name",
                                text(&recreation.landing_relation_name),
                            ),
                            ("consumer_group", text(&recreation.consumer_group)),
                            ("drifted_settings", texts(&recreation.drifted_setting_names)),
                            (
                                "message",
                                text(&render_source_recreation_notice(recreation)),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "warnings",
            JsonValue::Array(
                plan.warnings
                    .iter()
                    .map(|warning| {
                        object(vec![
                            ("warning_code", text(&warning.warning_code)),
                            ("message", text(&warning.message)),
                            ("root_key", object_key(&warning.root_key)),
                            (
                                "target_key",
                                warning
                                    .target_key
                                    .as_ref()
                                    .map_or(JsonValue::Null, object_key),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
    ];
    if !row_error_policy_changes.is_empty() {
        payload.push((
            "row_error_policy_changes",
            JsonValue::Array(
                row_error_policy_changes
                    .iter()
                    .map(|change| {
                        object(vec![
                            ("model_key", logical(&change.model_key)),
                            ("desired_policy", text(&change.desired_policy)),
                            ("view", text(&change.view_name)),
                            ("dead_letter_view", text(&change.dead_letter_view_name)),
                            (
                                "operation",
                                text(if change.landing_repair_only {
                                    "repair_landing_view"
                                } else {
                                    "view_only"
                                }),
                            ),
                            ("replay_roots", JsonValue::Integer(JsonInteger::from(0_i64))),
                        ])
                    })
                    .collect(),
            ),
        ));
    }
    let mut rendered: String = dumps(
        &object(payload),
        &JsonDialect::Stdlib(StdlibJsonOptions::new(Some(2))),
    )
    .expect("a direct plan payload holds only text, booleans, integers and null");
    rendered.push('\n');
    rendered
}

/// `dict(pairs)`: each name keeps its first position and takes its last value.
fn settings_object(pairs: &[(String, String)]) -> JsonValue {
    let mut settings: Vec<(&str, &str)> = Vec::new();
    for (name, value) in pairs {
        match settings.iter_mut().find(|(known, _)| *known == name) {
            Some(slot) => slot.1 = value,
            None => settings.push((name, value)),
        }
    }
    object(
        settings
            .into_iter()
            .map(|(name, value)| (name, JsonValue::String(value.to_owned())))
            .collect(),
    )
}
