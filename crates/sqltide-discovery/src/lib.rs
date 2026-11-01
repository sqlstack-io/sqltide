//! Project discovery for SQLTide: configuration validation, interpolation, and source
//! inference. Plain Rust with no Python dependency; decoding TOML and YAML is the caller's.

pub mod byte_size;
pub mod config;
pub mod durations;
pub mod errors;
pub mod freshness;
pub mod interpolation;
pub mod pipeline_config;
pub mod retention;
pub mod source_registry;
pub mod sources;
pub mod text;
pub mod value;

/// The start of a `${...}` interpolation token.
pub const INTERPOLATION_TOKEN_START: &str = "${";
