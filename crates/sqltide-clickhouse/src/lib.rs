//! ClickHouse SQL rendering for SQLTide's adapter. Python keeps the adapter protocol, the
//! connection, and credential substitution; these functions only build SQL text.

pub mod comparison;
pub mod resources;

/// One rendering request the adapter cannot express in ClickHouse.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapabilityError(pub String);
