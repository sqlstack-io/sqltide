//! Strict YAML on `saphyr-parser`: one document without anchors or aliases, with plain scalars
//! resolved by PyYAML's YAML 1.1 rules so values keep the types `yaml.safe_load` gave them.

pub(crate) mod composer;
pub(crate) mod constructor;
pub(crate) mod models;
pub(crate) mod reader;
pub(crate) mod resolver;

pub use constructor::Value;
