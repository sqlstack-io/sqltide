//! The committed project config and its optional local overrides, read from a project directory.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::errors::ConfigError;
use crate::models::ConfigValue;
use crate::toml::main::load_toml::load_toml;

pub const PROJECT_CONFIG_FILE_NAME: &str = "sqltide_project.toml";
pub const LOCAL_CONFIG_FILE_NAME: &str = "sqltide_local.toml";
pub const LEGACY_PROJECT_CONFIG_FILE_NAME: &str = "streambuild_project.toml";
pub const LEGACY_LOCAL_CONFIG_FILE_NAME: &str = "streambuild_local.toml";

/// The config file paths of one project directory, as the caller spells them.
pub struct ConfigPaths {
    pub project: PathBuf,
    pub legacy_project: PathBuf,
    pub local: PathBuf,
    pub legacy_local: PathBuf,
}

/// One config file: its text with universal newlines, and its TOML document.
pub struct ConfigSource {
    pub contents: String,
    pub document: ConfigValue,
}

/// Why a project's config files could not be read.
#[derive(Debug)]
pub enum SourceError {
    /// A project config error with this message.
    Invalid(String),
    /// The file is not valid TOML.
    InvalidToml { path: PathBuf, error: ConfigError },
    /// The file could not be read.
    Read { path: PathBuf, error: io::Error },
    /// The file is not UTF-8; `bytes` are its contents.
    NotUtf8 { bytes: Vec<u8> },
}

/// A file's text as `Path.read_text(encoding="utf-8")` returns it, with universal newlines.
fn read_text(path: &Path) -> Result<String, SourceError> {
    let bytes: Vec<u8> = fs::read(path).map_err(|error| SourceError::Read {
        path: path.to_owned(),
        error,
    })?;
    match String::from_utf8(bytes) {
        Ok(text) => Ok(text.replace("\r\n", "\n").replace('\r', "\n")),
        Err(error) => Err(SourceError::NotUtf8 {
            bytes: error.into_bytes(),
        }),
    }
}

/// Refuse the pre-rename config files, then require the project config.
pub fn check_config_paths(paths: &ConfigPaths) -> Result<(), SourceError> {
    for (legacy, current, name) in [
        (
            &paths.legacy_project,
            &paths.project,
            PROJECT_CONFIG_FILE_NAME,
        ),
        (&paths.legacy_local, &paths.local, LOCAL_CONFIG_FILE_NAME),
    ] {
        if legacy.exists() {
            let detail: &str = if current.exists() {
                " Remove the old file."
            } else {
                ""
            };
            return Err(SourceError::Invalid(format!(
                "{} is not supported. Rename it to {name}.{detail}",
                legacy.display()
            )));
        }
    }
    if paths.project.exists() {
        return Ok(());
    }
    Err(SourceError::Invalid(format!(
        "Project config not found: {}. Run sqt inside a SQLTide project or pass --project-dir.",
        paths.project.display()
    )))
}

/// Read and parse one config file.
pub fn read_config_source(path: &Path) -> Result<ConfigSource, SourceError> {
    let contents: String = read_text(path)?;
    let document: ConfigValue = load_toml(&contents).map_err(|error| SourceError::InvalidToml {
        path: path.to_owned(),
        error,
    })?;
    Ok(ConfigSource { contents, document })
}

/// The nearest directory, from `path` (or its directory when `path` is not one) upward, that
/// holds a project config or a pre-rename one, which loading then refuses.
pub fn find_project_configuration_dir(path: &Path) -> Option<PathBuf> {
    let start: &Path = if path.is_dir() {
        path
    } else {
        path.parent().unwrap_or(path)
    };
    start
        .ancestors()
        .map(|candidate| {
            if candidate.as_os_str().is_empty() {
                Path::new(".")
            } else {
                candidate
            }
        })
        .find(|candidate| {
            candidate.join(PROJECT_CONFIG_FILE_NAME).exists()
                || candidate.join(LEGACY_PROJECT_CONFIG_FILE_NAME).exists()
        })
        .map(Path::to_path_buf)
}
