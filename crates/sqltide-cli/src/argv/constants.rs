//! The embedded `sqt` command table and `argparse`'s fixed vocabulary.

/// The exported `sqt` parser tree: every parser, group and action in `add_argument` order.
pub(crate) const COMMAND_TABLE: &str = include_str!("command_table.json");
/// `argparse.SUPPRESS` as a destination or metavar.
pub(crate) const SUPPRESS_DEST: &str = "==SUPPRESS==";
/// The placeholder of the installed version in the `--version` text.
pub(crate) const VERSION_PLACEHOLDER: &str = "{version}";
pub(crate) const PREFIX_CHARS: char = '-';
pub(crate) const OPTIONS_TERMINATOR: &str = "--";
pub(crate) const USAGE_PREFIX: &str = "usage: ";
pub(crate) const PARSER_ERROR_EXIT: i64 = 2;
pub(crate) const HELP_EXIT: i64 = 0;
pub(crate) const INDENT_INCREMENT: i64 = 2;
pub(crate) const MAX_HELP_POSITION: i64 = 24;
pub(crate) const MINIMUM_TEXT_WIDTH: i64 = 11;
/// `_long_break_matcher` folds longer runs of newlines into this many.
pub(crate) const MAXIMUM_BLANK_RUN: usize = 2;
/// The `type=pathlib.Path` converter, which never rejects a string.
pub(crate) const PATH_CONVERTER: &str = "Path";
