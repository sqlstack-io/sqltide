//! Pipeline settings read before discovery validates the project.

use std::fs;
use std::path::Path;

use crate::models::ConfigValue;
use crate::toml::main::load_toml::load_toml;

const PIPELINE_CONFIG_FILE_NAME: &str = "pipeline.toml";
const VIRTUAL_MODE: &str = "virtual";

/// Python's universal newlines, which the commands read config files with.
fn universal_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

fn sets_virtual_mode(document: &ConfigValue) -> bool {
    matches!(document, ConfigValue::Map(entries) if entries.iter().any(|(key, value)| {
        key == "mode" && matches!(value, ConfigValue::String(mode) if mode == VIRTUAL_MODE)
    }))
}

/// Whether any `<pipelines_root>/*/pipeline.toml` sets `mode = "virtual"`. A missing root and
/// unreadable or invalid files count as not virtual: discovery reports those files later.
pub fn declares_virtual_pipeline(pipelines_root: &Path) -> bool {
    let Ok(entries) = fs::read_dir(pipelines_root) else {
        return false;
    };
    entries.flatten().any(|entry| {
        fs::read(entry.path().join(PIPELINE_CONFIG_FILE_NAME))
            .ok()
            .and_then(|bytes| String::from_utf8(bytes).ok())
            .and_then(|text| load_toml(&universal_newlines(&text)).ok())
            .is_some_and(|document| sets_virtual_mode(&document))
    })
}
