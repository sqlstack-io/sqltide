//! The argument text of one authored `@name(...)` macro call, read as Python's call grammar for
//! the literal subset macro arguments use, with nested macro calls standing in for values.
//!
//! Adapted from Apache-2.0 SQLBuild `crates/sqlbuild-render/src/macro_arguments` at 6a6aeab7b.
//! Positions count characters, not bytes.

mod failures;
mod lexer;
mod literals;
mod syntax_tree;

pub use failures::Failure;
pub use lexer::NumberToken;
pub use syntax_tree::{CallArguments, Keyword, Node};

/// Unicode lookups the host Python answers, so names match CPython's own tables.
pub trait ArgumentHost {
    /// `unicodedata.lookup(name)`, several characters for a named sequence, or `None`.
    fn character_named(&self, name: &str) -> Option<String>;
    /// The NFKC form of a non-ASCII identifier, or `None` when Python rejects it as one.
    fn identifier(&self, text: &str) -> Option<String>;
}

/// Parse `text` as the arguments of one call; `nested` are the `[start, end)` spans of nested
/// macro calls, each read as one value.
///
/// # Errors
///
/// Returns the first failure Python's tokenizer or parser would report.
pub fn parse_macro_arguments(
    host: &dyn ArgumentHost,
    text: &[char],
    nested: &[(usize, usize)],
) -> Result<CallArguments, Failure> {
    lexer::tokenize(host, text, nested)
        .and_then(|tokens| syntax_tree::parse_call_arguments(&tokens))
}
