"""Root CLI entrypoint."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError, AdapterWarehouseError
from sqltide.cli.admin.main._run_admin import run_admin_command
from sqltide.cli.entry._helpers.adapter_connection import (
    resolve_invocation_connection,
    resolve_observation_connection,
    resolve_primary_connection,
)
from sqltide.cli.entry._helpers.destruction import require_interactive_destruction
from sqltide.cli.entry._helpers.dispatch import dispatch_cli_command
from sqltide.cli.entry._helpers.entrypoint import (
    argv_for_parse_args,
    raise_keyboard_interrupt_from_signal,
)
from sqltide.cli.entry._helpers.invocation import resolve_cli_invocation
from sqltide.cli.entry._helpers.mode import validate_cli_command_mode
from sqltide.cli.entry._helpers.parser import build_cli_parser
from sqltide.cli.entry._helpers.user_error_rendering import render_cli_user_error
from sqltide.cli.entry.constants import DISPLAY_NAME_BY_COMMAND
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.main._errors import render_expected_warehouse_error
from sqltide.cli.entry.models import (
    CliEntrypointHandlers,
    ResolvedCliInvocation,
    ResolvedInvocationConnection,
)
from sqltide.cli.entry.types import CliCommand
from sqltide.compiler.compile.exceptions import TransformSqlContractError
from sqltide.diagnostics.main.attach_error_diagnostic import attach_error_diagnostic
from sqltide.diagnostics.main.render_error import render_error
from sqltide.diagnostics.types import DiagnosticPhase
from sqltide.executor.destruction.exceptions import DestructionError


def main(argv: Sequence[str] | None = None) -> int:
    from sqltide.cli.audit.main._run_audit import run_audit
    from sqltide.cli.build.main._run_build import run_build
    from sqltide.cli.compile.main._run_compile import run_compile
    from sqltide.cli.deployment.main._run_deployment_diff import run_deployment_diff
    from sqltide.cli.deployment.main._run_deployment_list import run_deployment_list
    from sqltide.cli.deployment.main._run_deployment_show import run_deployment_show
    from sqltide.cli.destruction.main._run_destruction import run_destruction
    from sqltide.cli.dev.main._run_dev import run_dev
    from sqltide.cli.discover.main._run_discover import run_discover
    from sqltide.cli.doctor.main._run_doctor import run_doctor
    from sqltide.cli.janitor.main._run_janitor import run_janitor
    from sqltide.cli.plan.main._run_plan import run_plan
    from sqltide.cli.promotion.main._run_deployment_promotion import (
        run_deployment_promotion,
    )
    from sqltide.cli.readiness.main._run_deployment_audit import run_deployment_audit
    from sqltide.cli.reconcile.main._run_reconcile import run_reconcile
    from sqltide.cli.repair_active_view.main._run_repair_active_view import (
        run_repair_active_view,
    )
    from sqltide.cli.rollback.main._run_deployment_rollback import run_deployment_rollback
    from sqltide.cli.test.main._run_test import run_test

    handlers: CliEntrypointHandlers = CliEntrypointHandlers(
        run_discover=run_discover,
        run_compile=run_compile,
        run_test=run_test,
        run_audit=run_audit,
        run_plan=run_plan,
        run_build=run_build,
        run_destruction=run_destruction,
        run_deployment_diff=run_deployment_diff,
        run_deployment_list=run_deployment_list,
        run_deployment_show=run_deployment_show,
        run_deployment_audit=run_deployment_audit,
        run_deployment_promote=run_deployment_promotion,
        run_deployment_rollback=run_deployment_rollback,
        run_reconcile=run_reconcile,
        run_janitor=run_janitor,
        run_doctor=run_doctor,
        run_dev=run_dev,
        run_repair_active_view=run_repair_active_view,
    )
    previous_sigterm_handler: Callable[[int, FrameType | None], object] | int | None = (
        signal.getsignal(signal.SIGTERM)
    )
    signal.signal(signal.SIGTERM, raise_keyboard_interrupt_from_signal)
    try:
        return _main_with_dependencies(argv=argv, handlers=handlers)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)


def _main_with_dependencies(
    *,
    argv: Sequence[str] | None = None,
    handlers: CliEntrypointHandlers,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
    adapter_connection: AdapterConnection | None = None,
    observation_adapter_connection: AdapterConnection | None = None,
) -> int:
    parser: argparse.ArgumentParser = build_cli_parser()
    args: argparse.Namespace = parser.parse_args(argv_for_parse_args(argv))
    try:
        require_interactive_destruction(args=args)
        resolved_database: str | None = None
        if args.command == CliCommand.PLAYGROUND:
            from sqltide.cli.playground.main._run_playground import run_playground

            return run_playground(
                directory=args.directory,
                working_directory=Path.cwd() if working_directory is None else working_directory,
            )
        if args.command == CliCommand.ADMIN:
            return run_admin_command(
                args=args,
                environment=os.environ if environment is None else environment,
                working_directory=Path.cwd() if working_directory is None else working_directory,
            )
        invocation: ResolvedCliInvocation = resolve_cli_invocation(
            args=args,
            environment=environment,
            working_directory=working_directory,
        )
        validate_cli_command_mode(invocation=invocation)
        resolved_database = invocation.database
        resolved_connection: ResolvedInvocationConnection
        connection_factory: Callable[[], AdapterConnection] | None
        observation_connection_factory: Callable[[], AdapterConnection] | None
        (
            resolved_connection,
            connection_factory,
            observation_connection_factory,
        ) = resolve_primary_connection(
            invocation=invocation,
            provided_connection=adapter_connection,
            resolver=resolve_invocation_connection,
        )
        observation_connection: ResolvedInvocationConnection | None = None
        try:
            observation_connection = resolve_observation_connection(
                invocation=invocation,
                provided_primary_connection=adapter_connection,
                provided_observation_connection=observation_adapter_connection,
                resolver=resolve_invocation_connection,
            )
            return dispatch_cli_command(
                invocation=invocation,
                handlers=handlers,
                adapter_connection=resolved_connection.connection,
                observation_connection=(
                    None if observation_connection is None else observation_connection.connection
                ),
                connection_factory=connection_factory,
                observation_connection_factory=observation_connection_factory,
            )
        finally:
            if (
                observation_connection is not None
                and observation_connection.close_after_command
                and observation_connection.connection is not None
            ):
                observation_connection.connection.close()
            if (
                resolved_connection.close_after_command
                and resolved_connection.connection is not None
            ):
                resolved_connection.connection.close()
    except CliUserError as error:
        print(render_cli_user_error(error), file=sys.stderr)
        return 1
    except (DestructionError, TransformSqlContractError, ValueError) as error:
        print(render_error(error), file=sys.stderr)
        return 1
    except AdapterWarehouseError as error:
        rendered_error: str | None = render_expected_warehouse_error(
            command_name=_command_name(args),
            database=resolved_database or "<unknown>",
            error=error,
        )
        if rendered_error is not None:
            print(rendered_error, file=sys.stderr)
            return 1
        raise
    except AdapterError as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.RUNTIME,
            code="SQT-RUNTIME-001",
        )
        print(render_error(error), file=sys.stderr)
        return 1


def _command_name(args: argparse.Namespace) -> str:
    """Return the operator-facing command name for error messages."""

    if args.command == CliCommand.DEPLOYMENT:
        return f"deployment {args.deployment_command}"
    return DISPLAY_NAME_BY_COMMAND.get(args.command, str(args.command))
