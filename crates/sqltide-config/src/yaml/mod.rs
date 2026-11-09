//! YAML 1.1 loading with the behaviour of PyYAML 6.0.3's pure-Python `SafeLoader`: the same
//! values, the same errors and the same marks.

pub(crate) mod _helpers;
pub(crate) mod composer;
pub(crate) mod constructor;
pub(crate) mod errors;
pub(crate) mod parser;
pub(crate) mod reader;
pub(crate) mod scanner;

pub use constructor::Value;
pub use errors::YamlError;
pub use reader::Mark;

/// The event parser of `text`; a character PyYAML's reader refuses fails here.
pub(crate) fn parser(text: &str) -> Result<parser::Parser, YamlError> {
    Ok(parser::Parser::new(scanner::Scanner::new(
        reader::Reader::new(text)?,
    )))
}
