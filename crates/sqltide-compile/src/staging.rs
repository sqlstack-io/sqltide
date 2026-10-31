//! Write compile artifacts into a fresh staging directory, refusing any path that would leave it.

use std::fmt;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

/// Why staging stopped: an artifact path outside the staging root, or a filesystem failure.
#[derive(Debug)]
pub enum StagingError {
    Escapes(String),
    Io(io::Error),
}

impl fmt::Display for StagingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Escapes(path) => write!(
                formatter,
                "Compile artifact path '{path}' escapes the target root"
            ),
            Self::Io(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for StagingError {}

impl From<io::Error> for StagingError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// `staging_root` joined with `relative_path`, when that names an entry strictly inside it.
///
/// The staging root is freshly created and holds only directories this module makes, so a
/// lexical check is exact: a rooted or prefixed path, a `..` segment, or a path with no normal
/// segment (which names the root itself) would escape.
///
/// # Errors
///
/// Returns [`StagingError::Escapes`] for any path that does not stay strictly inside the root.
pub fn staged_path(staging_root: &Path, relative_path: &str) -> Result<PathBuf, StagingError> {
    let mut has_segment: bool = false;
    for component in Path::new(relative_path).components() {
        match component {
            Component::Normal(_) => has_segment = true,
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(StagingError::Escapes(relative_path.to_owned()));
            }
        }
    }
    if !has_segment {
        return Err(StagingError::Escapes(relative_path.to_owned()));
    }
    Ok(staging_root.join(relative_path))
}

/// Write every `(relative path, contents)` file under `staging_root`, creating directories.
///
/// # Errors
///
/// Returns the first escaping path, checked before anything is written, or the first
/// filesystem failure.
pub fn stage_files(staging_root: &Path, files: &[(String, String)]) -> Result<(), StagingError> {
    let paths: Vec<PathBuf> = files
        .iter()
        .map(|(relative_path, _)| staged_path(staging_root, relative_path))
        .collect::<Result<_, _>>()?;
    for (path, (_, contents)) in paths.iter().zip(files) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, contents)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{StagingError, staged_path};
    use std::path::Path;

    #[test]
    fn paths_that_leave_or_name_the_root_are_rejected() {
        let root: &Path = Path::new("/staging");
        for escaping in ["../x.sql", "a/../../x.sql", "/abs.sql", "", ".", "./"] {
            assert!(
                matches!(staged_path(root, escaping), Err(StagingError::Escapes(_))),
                "{escaping:?} must be rejected"
            );
        }
        assert_eq!(
            staged_path(root, "compiled/./models/a.sql").expect("stays inside"),
            Path::new("/staging/compiled/./models/a.sql")
        );
    }
}
