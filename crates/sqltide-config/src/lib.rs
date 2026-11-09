//! Authored configuration for SQLTide: strict YAML on `saphyr-parser` with PyYAML's scalar
//! types, and the project access policy compiled from it.

pub mod access;
pub mod errors;
pub mod yaml;

#[cfg(test)]
mod tests;
