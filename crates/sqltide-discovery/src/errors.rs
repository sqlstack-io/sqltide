//! Configuration errors, each raised as one Python exception class.

/// Which exception a configuration error raises.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorKind {
    ProjectConfig,
    DurationParse,
    RetentionConfig,
    PipelineDiscovery,
}

impl ErrorKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ProjectConfig => "project_config",
            Self::DurationParse => "duration_parse",
            Self::RetentionConfig => "retention_config",
            Self::PipelineDiscovery => "pipeline_discovery",
        }
    }
}

/// One configuration error.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpecError {
    pub kind: ErrorKind,
    pub message: String,
}

impl SpecError {
    #[must_use]
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    /// The same message raised as another kind, as Python's `raise X(str(error))` does.
    #[must_use]
    pub fn reraise(self, kind: ErrorKind) -> Self {
        Self { kind, ..self }
    }
}

/// A configuration result.
pub type SpecResult<T> = Result<T, SpecError>;

/// A project configuration error.
pub fn project(message: impl Into<String>) -> SpecError {
    SpecError::new(ErrorKind::ProjectConfig, message)
}
