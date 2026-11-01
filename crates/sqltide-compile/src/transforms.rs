//! The compile contract of one transform or view: which reference annotations it may use,
//! whether it reads its driving source, and whether its output can anchor a replay.

const REF_RELATION: &str = "ref";
const MUTABLE_REF_TYPE: &str = "mutable";
const NEVER_REPLAY_ANCHOR: &str = "never";

/// `(name, relation type, ref type)` of one reference in a model query.
pub type Reference<'a> = (&'a str, &'a str, Option<&'a str>);

/// What a valid transform's references and output decide.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransformContract {
    pub preserves_required_lineage: bool,
    pub replay_anchor_eligible: bool,
}

/// The output columns each replay lineage mode requires.
#[must_use]
pub fn required_lineage_columns(replay_lineage_mode: &str) -> &'static [&'static str] {
    match replay_lineage_mode {
        "offsets" => &["_replay_partition", "_replay_offset"],
        "timestamp" => &["_replay_timestamp"],
        "landed_at" => &["_replay_landed_at"],
        "cursor" => &["_replay_cursor"],
        _ => &[],
    }
}

/// Check a transform's references and decide its lineage and replay-anchor eligibility.
///
/// # Errors
///
/// Returns the first invalid reference in query order, then a missing driving source.
pub fn transform_contract(
    transform_name: &str,
    source: &str,
    references: &[Reference<'_>],
    output_column_names: &[&str],
    replay_lineage_mode: &str,
    replay_anchor: &str,
    has_aggregate_semantics: bool,
) -> Result<TransformContract, String> {
    for (name, relation_type, ref_type) in references {
        if *name == source {
            if ref_type.is_some() {
                return Err(format!(
                    "Transform '{transform_name}' must not declare ref_type for its driving input \
                     '{source}'"
                ));
            }
            continue;
        }
        if ref_type.is_none() {
            return Err(format!(
                "Transform '{transform_name}' must declare ref_type for additional ref '{name}'"
            ));
        }
        if *relation_type != REF_RELATION {
            return Err(format!(
                "Transform '{transform_name}' must reference additional dependency '{name}' with \
                 __ref(...)"
            ));
        }
    }
    if !references.iter().any(|(name, _, _)| *name == source) {
        return Err(format!(
            "Transform '{transform_name}' must reference its source '{source}' in SQL"
        ));
    }
    let has_mutable_refs: bool = references.iter().any(|(name, relation_type, ref_type)| {
        *relation_type == REF_RELATION && *name != source && *ref_type == Some(MUTABLE_REF_TYPE)
    });
    let preserves_required_lineage: bool = required_lineage_columns(replay_lineage_mode)
        .iter()
        .all(|column| output_column_names.contains(column));
    Ok(TransformContract {
        preserves_required_lineage,
        replay_anchor_eligible: replay_anchor != NEVER_REPLAY_ANCHOR
            && !has_mutable_refs
            && !has_aggregate_semantics
            && preserves_required_lineage,
    })
}

/// The message for a view whose references declare a ref type, naming them sorted and once.
#[must_use]
pub fn view_ref_type_error(view_name: &str, references: &[Reference<'_>]) -> Option<String> {
    let mut typed: Vec<&str> = references
        .iter()
        .filter(|(_, _, ref_type)| ref_type.is_some())
        .map(|(name, _, _)| *name)
        .collect();
    typed.sort_unstable();
    typed.dedup();
    (!typed.is_empty()).then(|| {
        format!(
            "View '{view_name}' must not declare ref_type annotations for: {}",
            typed.join(", ")
        )
    })
}
