//! Target-relative artifact paths for compiled models, resources, audits, and SQL tests.
//!
//! Every path is POSIX text under `compiled/`. Each authored name becomes exactly one path
//! segment, so a name that is empty, `.`, `..`, or holds a separator is rejected rather than
//! allowed to change the directory layout.

use std::collections::BTreeSet;
use std::fmt;

/// The authored identity a path segment comes from, named in rejection messages.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SegmentKind {
    Pipeline,
    Model,
    Source,
    AdapterResource,
    Audit,
    AuditFile,
    SqlTest,
    TestTarget,
}

impl SegmentKind {
    const fn label(self) -> &'static str {
        match self {
            Self::Pipeline => "pipeline",
            Self::Model => "model",
            Self::Source => "source",
            Self::AdapterResource => "adapter resource",
            Self::Audit => "audit",
            Self::AuditFile => "audit file",
            Self::SqlTest => "SQL test",
            Self::TestTarget => "test target",
        }
    }
}

/// A name that cannot be used as one artifact path segment.
#[derive(Debug, Eq, PartialEq)]
pub struct UnsafeSegment {
    pub kind: SegmentKind,
    pub value: String,
}

impl fmt::Display for UnsafeSegment {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "Unsafe {} artifact path segment '{}'",
            self.kind.label(),
            self.value
        )
    }
}

impl std::error::Error for UnsafeSegment {}

/// The adapter resource a model file renders.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelResource {
    Table,
    View,
    MaterializedView,
}

impl ModelResource {
    const fn suffix(self) -> &'static str {
        match self {
            Self::Table => "table",
            Self::View => "view",
            Self::MaterializedView => "mv",
        }
    }
}

/// `value` unchanged when it is safe as a single path segment on every platform.
///
/// An absolute path always holds a separator, so rejecting separators also rejects those.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] for an empty name, `.`, `..`, or a name holding `/` or `\`.
pub fn safe_segment(value: &str, kind: SegmentKind) -> Result<&str, UnsafeSegment> {
    if value.is_empty() || value == "." || value == ".." || value.contains(['/', '\\']) {
        return Err(UnsafeSegment {
            kind,
            value: value.to_owned(),
        });
    }
    Ok(value)
}

/// `compiled/models/<pipeline>/<model>.sql`, the model's resolved query.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] when either name is not a safe segment.
pub fn model_query_path(pipeline: &str, model: &str) -> Result<String, UnsafeSegment> {
    Ok(format!(
        "compiled/models/{}/{}.sql",
        safe_segment(pipeline, SegmentKind::Pipeline)?,
        safe_segment(model, SegmentKind::Model)?
    ))
}

/// `compiled/resources/models/<pipeline>/<model>.<table|view|mv>.sql`.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] when either name is not a safe segment.
pub fn model_resource_path(
    pipeline: &str,
    model: &str,
    resource: ModelResource,
) -> Result<String, UnsafeSegment> {
    Ok(format!(
        "compiled/resources/models/{}/{}.{}.sql",
        safe_segment(pipeline, SegmentKind::Pipeline)?,
        safe_segment(model, SegmentKind::Model)?,
        resource.suffix()
    ))
}

/// `compiled/resources/sources/<source>/<resource>.sql`.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] when either name is not a safe segment.
pub fn source_resource_path(source: &str, resource: &str) -> Result<String, UnsafeSegment> {
    Ok(format!(
        "compiled/resources/sources/{}/{}.sql",
        safe_segment(source, SegmentKind::Source)?,
        safe_segment(resource, SegmentKind::AdapterResource)?
    ))
}

/// `compiled/audits/<authored folders>/<file stem>[__<name>].sql`.
///
/// `relative_file` is the audit file's POSIX path relative to the project's `audits/` folder
/// (or the project root), so its folders mirror the authored layout.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] when the file stem or the audit name is not a safe segment.
pub fn audit_path(relative_file: &str, name: Option<&str>) -> Result<String, UnsafeSegment> {
    let (folders, file_name): (Option<&str>, &str) = match relative_file.rsplit_once('/') {
        Some((folders, file_name)) => (Some(folders), file_name),
        None => (None, relative_file),
    };
    let name: Option<&str> = name
        .map(|name| safe_segment(name, SegmentKind::Audit))
        .transpose()?;
    let stem: &str = safe_segment(file_stem(file_name), SegmentKind::AuditFile)?;
    let mut path: String = String::from("compiled/audits/");
    if let Some(folders) = folders {
        path.push_str(folders);
        path.push('/');
    }
    path.push_str(stem);
    if let Some(name) = name {
        path.push_str("__");
        path.push_str(name);
    }
    path.push_str(".sql");
    Ok(path)
}

/// `compiled/tests/<folder>/<name>.sql`, where the folder is the single target model, the
/// sorted distinct targets under `_chain_/` joined by `__`, or the test name when untargeted.
///
/// # Errors
///
/// Returns [`UnsafeSegment`] when the test name or any target name is not a safe segment.
pub fn static_test_path<'a>(
    name: &str,
    target_names: impl IntoIterator<Item = &'a str>,
) -> Result<String, UnsafeSegment> {
    let name: &str = safe_segment(name, SegmentKind::SqlTest)?;
    let targets: BTreeSet<&str> = target_names
        .into_iter()
        .map(|target| safe_segment(target, SegmentKind::TestTarget))
        .collect::<Result<_, _>>()?;
    let folder: String = match targets.len() {
        0 => name.to_owned(),
        1 => targets.into_iter().collect(),
        _ => format!(
            "_chain_/{}",
            targets.into_iter().collect::<Vec<_>>().join("__")
        ),
    };
    Ok(format!("compiled/tests/{folder}/{name}.sql"))
}

/// The file name without its final suffix, as Python's `PurePath.stem` computes it: a leading
/// dot or a trailing dot is not a suffix.
fn file_stem(file_name: &str) -> &str {
    match file_name.rfind('.') {
        Some(index) if index > 0 && index + 1 < file_name.len() => &file_name[..index],
        _ => file_name,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_names_that_would_change_the_layout() {
        for value in ["", ".", "..", "a/b", "/tmp/x", "a\\b", "C:\\x"] {
            assert_eq!(
                model_query_path("p", value),
                Err(UnsafeSegment {
                    kind: SegmentKind::Model,
                    value: value.to_owned()
                })
            );
        }
        assert_eq!(
            model_query_path("p", "...").as_deref(),
            Ok("compiled/models/p/....sql")
        );
    }

    #[test]
    fn audit_stem_follows_python_suffix_rules() {
        assert_eq!(
            audit_path("orders/checks.v2.sql", Some("not null")).as_deref(),
            Ok("compiled/audits/orders/checks.v2__not null.sql")
        );
        assert_eq!(
            audit_path(".sql", None).as_deref(),
            Ok("compiled/audits/.sql.sql")
        );
        assert_eq!(
            audit_path("check.", None).as_deref(),
            Ok("compiled/audits/check..sql")
        );
        assert_eq!(
            audit_path("check", None).as_deref(),
            Ok("compiled/audits/check.sql")
        );
    }

    #[test]
    fn test_folder_sorts_and_deduplicates_chain_targets() {
        assert_eq!(
            static_test_path("t", ["zeta", "alpha", "zeta", "beta"]).as_deref(),
            Ok("compiled/tests/_chain_/alpha__beta__zeta/t.sql")
        );
        assert_eq!(
            static_test_path("t", ["orders", "orders"]).as_deref(),
            Ok("compiled/tests/orders/t.sql")
        );
        assert_eq!(
            static_test_path("t", []).as_deref(),
            Ok("compiled/tests/t/t.sql")
        );
    }
}
