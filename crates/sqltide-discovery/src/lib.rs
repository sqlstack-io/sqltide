//! Project discovery for SQLTide: configuration validation, interpolation, and source
//! inference. Plain Rust with no Python dependency; decoding TOML and YAML is the caller's.

pub mod audits;
pub mod byte_size;
pub mod config;
pub mod declarations;
pub mod durations;
pub mod effective;
pub mod errors;
pub mod freshness;
pub mod interpolation;
pub mod model_spec;
pub mod pipeline_config;
pub mod project_validation;
pub mod retention;
pub mod source_registry;
pub mod sources;
pub mod sql_test_blocks;
pub mod sql_test_classification;
pub mod text;
pub mod value;

/// The start of a `${...}` interpolation token.
pub const INTERPOLATION_TOKEN_START: &str = "${";
