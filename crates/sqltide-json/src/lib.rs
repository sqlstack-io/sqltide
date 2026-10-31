//! Serialize JSON byte-for-byte as Python's `json.dumps` does.
//!
//! Copied from Apache-2.0 SQLBuild `sqlbuild-core/src/json` at 6a6aeab7b.

pub(crate) mod _helpers;
pub(crate) mod constants;
pub mod errors;
pub mod serialize;

pub use errors::JsonEmitError;
pub use models::{JsonDialect, JsonInteger, JsonKey, JsonValue, StdlibJsonOptions};
pub use serialize::dumps::dumps;
pub mod models;

#[cfg(test)]
mod tests;
