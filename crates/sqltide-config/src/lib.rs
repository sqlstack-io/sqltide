//! Authored configuration for SQLTide: YAML loaded with PyYAML's safe-loader semantics, and the
//! project access policy compiled from it.

pub mod access;
pub mod yaml;

#[cfg(test)]
mod tests;
