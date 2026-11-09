//! Authored configuration for SQLTide: TOML 1.0 with `tomllib`'s values, strict YAML on
//! `saphyr-parser` with PyYAML's scalar types, and the project access policy compiled from it.

pub mod access;
pub(crate) mod constants;
pub mod errors;
pub mod models;
pub mod toml;
pub mod yaml;

#[cfg(test)]
mod tests;
