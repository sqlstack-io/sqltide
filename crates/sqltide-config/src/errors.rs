//! Structured failures of reading configuration files.

/// Why a configuration document could not be loaded.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConfigErrorKind {
    /// The text is not a valid document.
    Syntax,
    /// The document parses but a value cannot be built from it.
    Construct,
    /// The document nests deeper than the loader reads.
    TooDeep,
}

/// A configuration failure, at its one-based line and column where known.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    pub kind: ConfigErrorKind,
    pub message: String,
    pub line: Option<usize>,
    pub column: Option<usize>,
}

impl ConfigError {
    /// An error without a position.
    pub fn new(kind: ConfigErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            line: None,
            column: None,
        }
    }

    /// The message with `tomllib.TOMLDecodeError`'s location suffix.
    pub fn tomllib_text(&self) -> String {
        match (self.line, self.column) {
            (Some(line), Some(column)) => {
                format!("{} (at line {line}, column {column})", self.message)
            }
            _ => format!("{} (at end of document)", self.message),
        }
    }

    /// This error at a one-based line and column, unless it already has a position.
    #[must_use]
    pub fn at(self, line: usize, column: usize) -> Self {
        if self.line.is_some() {
            return self;
        }
        Self {
            line: Some(line),
            column: Some(column),
            ..self
        }
    }
}
