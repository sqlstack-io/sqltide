//! Failures of loading a YAML document.

use crate::yaml::reader::Mark;

/// A failure at a mark: PyYAML's `MarkedYAMLError` problem, or, when `internal` is set, a
/// failure PyYAML does not report as a YAML error (a rejected character or a constructor that
/// raises a plain Python exception).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct YamlError {
    pub problem: String,
    pub mark: Mark,
    pub internal: bool,
}

impl YamlError {
    pub(crate) fn marked(problem: impl Into<String>, mark: Mark) -> Self {
        Self {
            problem: problem.into(),
            mark,
            internal: false,
        }
    }

    pub(crate) fn internal(problem: impl Into<String>, mark: Mark) -> Self {
        Self {
            problem: problem.into(),
            mark,
            internal: true,
        }
    }
}
