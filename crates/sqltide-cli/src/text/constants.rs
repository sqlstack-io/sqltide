//! Fixed values of Python's text rules.

/// The code points in one run of Unicode decimal digits, valued 0 to 9.
pub(crate) const DIGITS_PER_RUN: u32 = 10;

/// `textwrap`'s em-dash is at least this many hyphens.
pub(crate) const MINIMUM_EM_DASH: usize = 2;

/// The tab stop `str.expandtabs` uses by default.
pub(crate) const TAB_SIZE: usize = 8;
