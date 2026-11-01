"""CLI entry constants."""

from collections.abc import Mapping

from sqltide.cli.entry.types import CliCommand, CliSubcommand

ACTIVE_VIEW_SUBCOMMAND: str = CliSubcommand.ACTIVE_VIEW
DEV_CLI_VARIABLES_ENV_VAR: str = "SQLTIDE_INTERNAL_CLI_VARS"

COMMANDS_REQUIRING_PIPELINES_ROOT: frozenset[CliCommand] = frozenset(
    {
        CliCommand.DEV,
        CliCommand.DISCOVER,
        CliCommand.COMPILE,
        CliCommand.LINT,
        CliCommand.FORMAT,
        CliCommand.TEST,
        CliCommand.PLAN,
        CliCommand.BUILD,
        CliCommand.DESTROY,
        CliCommand.RESET_TARGET,
        CliCommand.DEPLOYMENT,
        CliCommand.RECONCILE,
        CliCommand.AUDIT,
    }
)
COMMANDS_WITHOUT_ADAPTER_CONNECTION: frozenset[CliCommand] = frozenset(
    {CliCommand.DISCOVER, CliCommand.COMPILE, CliCommand.LINT, CliCommand.FORMAT}
)
DIRECT_ONLY_COMMANDS: frozenset[CliCommand] = frozenset()
VIRTUAL_ENVIRONMENT_ONLY_COMMANDS: frozenset[CliCommand] = frozenset(
    {
        CliCommand.DEPLOYMENT,
        CliCommand.RECONCILE,
        CliCommand.JANITOR,
        CliCommand.DOCTOR,
        CliCommand.REPAIR,
    }
)

DISPLAY_NAME_BY_COMMAND: Mapping[CliCommand, str] = {
    CliCommand.AUDIT: "audit",
    CliCommand.DESTROY: "destroy",
    CliCommand.REPAIR: "repair active-view",
    CliCommand.RESET_TARGET: "reset-target",
}

METADATA_TABLE_NAME_PREFIX: str = "sqltide_"
AFFIRMATIVE_RESPONSES: frozenset[str] = frozenset({"y", "yes"})
TRUE_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
