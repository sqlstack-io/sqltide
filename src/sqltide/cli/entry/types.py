"""CLI entry runtime domain types."""

from enum import StrEnum


class CliCommand(StrEnum):
    """One top-level `sqt` subcommand as argparse reports it."""

    ADMIN = "admin"
    DISCOVER = "discover"
    DEV = "dev"
    COMPILE = "compile"
    LINT = "lint"
    FORMAT = "format"
    TEST = "test"
    PLAN = "plan"
    BUILD = "build"
    DESTROY = "destroy"
    RESET_TARGET = "reset-target"
    DEPLOYMENT = "deployment"
    AUDIT = "audit"
    RECONCILE = "reconcile"
    JANITOR = "janitor"
    DOCTOR = "doctor"
    REPAIR = "repair"


class CliSubcommand(StrEnum):
    """One nested `sqt <command> <subcommand>` selector."""

    ACTIVE_VIEW = "active-view"
    DIFF = "diff"
    LIST = "list"
    SHOW = "show"
    AUDIT = "audit"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
