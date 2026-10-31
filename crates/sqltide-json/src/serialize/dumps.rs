//! Serialize JSON exactly as SQLBuild's Python serializers do.

use crate::errors::JsonEmitError;
use crate::models::{JsonDialect, JsonValue};

/// The text `json.dumps(value, ...)` or `orjson.dumps(value, ...).decode()` returns.
pub fn dumps(value: &JsonValue, dialect: &JsonDialect) -> Result<String, JsonEmitError> {
    crate::_helpers::emitter::emit(value, dialect)
}
