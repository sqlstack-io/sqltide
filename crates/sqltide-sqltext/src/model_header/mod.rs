//! The authored `MODEL (...)` header grammar: a map of `key value` pairs whose values are quoted
//! strings, bare words, `[...]` lists and `(...)` maps.
//!
//! Positions in error messages count characters, not bytes, so they match the Python
//! implementation this replaces.

mod parser;
mod tokenizer;
mod value;

pub use value::HeaderValue;

/// The deepest list or map nesting a header may use. Deeper input is rejected before recursion
/// could exhaust the stack.
pub const MAX_HEADER_NESTING_DEPTH: usize = 128;

/// Parse the text between `MODEL (` and its closing `)` into an ordered map.
///
/// # Errors
///
/// Returns the syntax error message for malformed header text.
pub fn parse_model_header(header: &str) -> Result<Vec<(String, HeaderValue)>, String> {
    let characters: Vec<char> = header.chars().collect();
    let tokens = tokenizer::tokenize(&characters, false)?;
    parser::parse(&tokens, false)
}

/// Parse the text between `CONSTANT (` or `ENUM (` and its closing `)`: the MODEL grammar plus
/// `{...}` sets, with bare non-literal words kept as `HeaderValue::Word`.
///
/// # Errors
///
/// Returns the syntax error message for malformed header text.
pub fn parse_declaration_header(header: &str) -> Result<Vec<(String, HeaderValue)>, String> {
    let characters: Vec<char> = header.chars().collect();
    let tokens = tokenizer::tokenize(&characters, true)?;
    parser::parse(&tokens, true)
}

#[cfg(test)]
mod tests;
