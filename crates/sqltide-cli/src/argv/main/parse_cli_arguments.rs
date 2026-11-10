//! Parse `sqt`'s argv as its `argparse` parser's `parse_args` did.

use crate::argv::_helpers::parsing::{Exit, KnownArgs, Run};
use crate::argv::_helpers::table::root_parser;
use crate::argv::models::{ArgvOutcome, ArgvSettings, Namespace, ParserSpec};
use crate::argv::types::ArgvHost;

/// `build_cli_parser().parse_args(argv)`: the namespace or exit status, and everything to print.
pub fn parse_cli_arguments(
    argv: &[String],
    settings: ArgvSettings,
    host: &mut dyn ArgvHost,
) -> ArgvOutcome {
    let parser: &ParserSpec = root_parser();
    let mut run: Run<'_> = Run {
        settings: &settings,
        host,
        writes: Vec::new(),
    };
    let parsed: Result<Namespace, i64> = run
        .parse_known_args(parser, argv.to_vec(), Namespace::default())
        .and_then(|(namespace, extras): KnownArgs| {
            if extras.is_empty() {
                return Ok(namespace);
            }
            let message: String = format!("unrecognized arguments: {}", extras.join(" "));
            Err(run.error(parser, &message))
        })
        .map_err(|exit: Exit| exit.0);
    ArgvOutcome {
        writes: run.writes,
        parsed,
    }
}
