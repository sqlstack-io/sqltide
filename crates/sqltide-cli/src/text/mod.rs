//! Python's text rules the command line reproduces: `textwrap.wrap`, `str.isalnum` and `\d`.

pub(crate) mod constants;
pub mod decimal_digits;
pub mod python_isalnum;
pub(crate) mod python_tables;
pub mod text_wrap;

#[cfg(test)]
mod tests;
