from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.auth.main.default_control_store_url import default_control_store_url
from sqltide.auth.models import AuthSettings
from sqltide.auth.types import AuthenticationMode, UnknownUserPolicy
from sqltide.cli.build.models import BuildCommandOptions
from sqltide.cli.destruction.models import DestructionCommandOptions
from sqltide.cli.dev.models import DevCommandOptions
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.cli.entry.constants import TRUE_ENV_VALUES
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.models import (
    CliEntrypointHandlers,
    ResolvedCliInvocation,
)
from sqltide.cli.entry.types import CliCommand, CliSubcommand
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.dev_server.constants import DEFAULT_DEV_SERVER_HOST, DEFAULT_DEV_SERVER_PORT
from sqltide.executor.destruction.types import DestructionOperation


def dispatch_cli_command(
    *,
    invocation: ResolvedCliInvocation,
    handlers: CliEntrypointHandlers,
    adapter_connection: AdapterConnection | None,
    observation_connection: AdapterConnection | None = None,
    connection_factory: Callable[[], AdapterConnection] | None = None,
    observation_connection_factory: Callable[[], AdapterConnection] | None = None,
) -> int:
    args: argparse.Namespace = invocation.args
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(invocation.adapter)
    if getattr(args, "no_sql_analysis", False):
        adapter_profile = replace(adapter_profile, run_sql_rules=False)
    if args.command in (CliCommand.LINT, CliCommand.FORMAT):
        from sqltide.cli.sql.main._run_sql import run_sql

        return run_sql(
            invocation=invocation,
            adapter_profile=adapter_profile,
        )
    if args.command == CliCommand.DISCOVER:
        return handlers.run_discover(
            pipelines_root=invocation.pipelines_root,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
        )
    if args.command == CliCommand.COMPILE:
        return handlers.run_compile(
            pipelines_root=invocation.pipelines_root,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
            target_dir=(
                getattr(args, "target_dir", None)
                or (
                    invocation.project_dir / "target"
                    if invocation.project_dir is not None
                    else None
                )
            ),
        )
    if args.command == CliCommand.DEV:
        effective_target: str | None = (
            None
            if invocation.loaded_project is None
            or invocation.loaded_project.effective_configuration is None
            else invocation.loaded_project.effective_configuration.target_name
        )
        project_dir: Path = invocation.project_dir or _require_pipelines_root(invocation).parent
        environment: Mapping[str, str] = invocation.connection.environment or {}
        auth_settings: AuthSettings = AuthSettings(
            mode=AuthenticationMode(
                getattr(args, "auth_mode", None)
                or environment.get("SQLTIDE_AUTH_MODE", AuthenticationMode.DISABLED)
            ),
            control_store_url=(
                getattr(args, "control_store_url", None)
                or environment.get("SQLTIDE_CONTROL_STORE_URL")
                or default_control_store_url(project_dir=project_dir)
            ),
            username_header=(
                getattr(args, "auth_username_header", None)
                or environment.get("SQLTIDE_AUTH_USERNAME_HEADER", "X-SQLTide-User")
            ),
            display_name_header=(
                getattr(args, "auth_display_name_header", None)
                or environment.get("SQLTIDE_AUTH_DISPLAY_NAME_HEADER")
            ),
            email_header=(
                getattr(args, "auth_email_header", None)
                or environment.get("SQLTIDE_AUTH_EMAIL_HEADER")
            ),
            unknown_user_policy=UnknownUserPolicy(
                getattr(args, "auth_unknown_user_policy", None)
                or environment.get(
                    "SQLTIDE_AUTH_UNKNOWN_USER_POLICY",
                    UnknownUserPolicy.AUTO_PROVISION,
                )
            ),
            default_role=(
                getattr(args, "auth_default_role", None)
                or environment.get("SQLTIDE_AUTH_DEFAULT_ROLE", "viewer")
            ),
            session_ttl_seconds=int(
                getattr(args, "auth_session_ttl_seconds", None)
                or environment.get("SQLTIDE_AUTH_SESSION_TTL_SECONDS", 43200)
            ),
            session_cookie_secure=(
                not bool(getattr(args, "auth_insecure_cookie", False))
                and environment.get("SQLTIDE_AUTH_INSECURE_COOKIE", "").casefold()
                not in TRUE_ENV_VALUES
            ),
            proxy_logout_url=(
                getattr(args, "auth_proxy_logout_url", None)
                or environment.get("SQLTIDE_AUTH_PROXY_LOGOUT_URL")
            ),
        )
        return handlers.run_dev(
            options=DevCommandOptions(
                pipelines_root=_require_pipelines_root(invocation),
                database=invocation.database,
                host=str(getattr(args, "ui_host", DEFAULT_DEV_SERVER_HOST)),
                port=int(getattr(args, "ui_port", DEFAULT_DEV_SERVER_PORT)),
                selected_target=effective_target or getattr(args, "target", None),
                cli_variables=tuple(getattr(args, "vars", {}).items()),
                environment=invocation.connection.environment,
                connection_host=invocation.connection.host,
                connection_port=invocation.connection.port,
                connection_username=invocation.connection.username,
                connection_password=invocation.connection.password,
                auth_settings=auth_settings,
            ),
            client=adapter_connection,
            observation_client=observation_connection,
            connection_factory=connection_factory,
            observation_connection_factory=observation_connection_factory,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
        )
    client: AdapterConnection = _resolve_connection(
        adapter_connection=adapter_connection,
    )
    if args.command == CliCommand.TEST:
        return handlers.run_test(
            pipelines_root=invocation.pipelines_root,
            project_dir=invocation.project_dir,
            selectors=tuple(getattr(args, "select", [])),
            paths=tuple(getattr(args, "paths", ())),
            verbose=bool(getattr(args, "verbose", False)),
            client=client,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
            target_dir=getattr(args, "target_dir", None),
            database=invocation.database or "",
        )
    if args.command == CliCommand.BUILD:
        if observation_connection is None:
            raise CliUserError("Build requires a dedicated observation connection")
        return handlers.run_build(
            options=BuildCommandOptions(
                pipelines_root=_require_pipelines_root(invocation),
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                selectors=tuple(getattr(args, "select", [])),
                deployment_id=getattr(args, "deployment_id", None),
                full_refresh=bool(getattr(args, "full_refresh", False)),
                start_time=getattr(args, "start_time", None),
                json_output=bool(getattr(args, "json", False)),
                verbose=bool(getattr(args, "verbose", False)),
                auto_approve=bool(getattr(args, "auto_approve", False)),
                events_output=bool(getattr(args, "events", False)),
                confirmations=tuple(getattr(args, "confirm", [])),
                changed=bool(getattr(args, "changed", False)),
                include_missing_upstream=bool(getattr(args, "include_missing_upstream", False)),
            ),
            client=client,
            observation_client=observation_connection,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
        )
    if args.command in {CliCommand.DESTROY, CliCommand.RESET_TARGET}:
        if observation_connection is None:
            raise CliUserError("Destruction requires a dedicated observation connection")
        project_dir: Path = invocation.project_dir or _require_pipelines_root(invocation).parent
        return handlers.run_destruction(
            options=DestructionCommandOptions(
                operation=(
                    DestructionOperation.DESTROY_PIPELINES
                    if args.command == CliCommand.DESTROY
                    else DestructionOperation.RESET_TARGET
                ),
                pipelines_root=_require_pipelines_root(invocation),
                project_dir=project_dir,
                selected_target=str(args.target),
                database=invocation.database,
                selectors=tuple(getattr(args, "select", ())),
                include_orphans=bool(getattr(args, "include_orphans", False)),
                control_store_url=(
                    getattr(args, "control_store_url", None)
                    or default_control_store_url(project_dir=project_dir)
                ),
                cli_variables=invocation.cli_variables,
                environment=invocation.connection.environment,
            ),
            client=client,
            observation_client=observation_connection,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
        )
    if args.command == CliCommand.DEPLOYMENT:
        deployment_command: str = str(args.deployment_command)
        if deployment_command == CliSubcommand.LIST:
            return handlers.run_deployment_list(
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                json_output=bool(getattr(args, "json", False)),
                client=client,
            )
        if deployment_command == CliSubcommand.SHOW:
            return handlers.run_deployment_show(
                deployment_id=args.deployment_id,
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                json_output=bool(getattr(args, "json", False)),
                client=client,
            )
        if deployment_command == CliSubcommand.DIFF:
            return handlers.run_deployment_diff(
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                comparison=args.comparison,
                json_output=bool(getattr(args, "json", False)),
                client=client,
            )
        if deployment_command == CliSubcommand.AUDIT:
            return handlers.run_deployment_audit(
                pipelines_root=invocation.pipelines_root,
                project_dir=(
                    invocation.project_dir
                    if invocation.project_dir is not None
                    else (
                        invocation.pipelines_root.parent
                        if invocation.pipelines_root is not None
                        else None
                    )
                ),
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                deployment_id=args.deployment_id,
                json_output=bool(getattr(args, "json", False)),
                client=client,
                loaded_project=invocation.loaded_project,
                adapter_profile=adapter_profile,
            )
        if deployment_command == CliSubcommand.ROLLBACK:
            return handlers.run_deployment_rollback(
                database=invocation.database,
                metadata_database=getattr(args, "metadata_database", None),
                deployment_id=getattr(args, "deployment_id", None),
                previous=bool(getattr(args, "previous", False)),
                auto_approve=bool(getattr(args, "auto_approve", False)),
                json_output=bool(getattr(args, "json", False)),
                client=client,
            )
        return handlers.run_deployment_promote(
            database=invocation.database,
            metadata_database=getattr(args, "metadata_database", None),
            deployment_id=args.deployment_id,
            json_output=bool(getattr(args, "json", False)),
            client=client,
        )
    if args.command == CliCommand.AUDIT:
        if invocation.pipelines_root is None:
            raise CliUserError("Audit command requires a resolved pipelines root")
        return handlers.run_audit(
            pipelines_root=invocation.pipelines_root,
            project_dir=invocation.project_dir or invocation.pipelines_root.parent,
            database=invocation.database,
            selectors=tuple(getattr(args, "select", [])),
            json_output=bool(getattr(args, "json", False)),
            client=client,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
            force=bool(getattr(args, "force", False)),
        )
    if args.command == CliCommand.RECONCILE:
        return handlers.run_reconcile(
            pipelines_root=invocation.pipelines_root,
            database=invocation.database,
            metadata_database=getattr(args, "metadata_database", None),
            selectors=tuple(getattr(args, "select", [])),
            json_output=bool(getattr(args, "json", False)),
            apply=bool(getattr(args, "apply", False)),
            client=client,
            loaded_project=invocation.loaded_project,
            adapter_profile=adapter_profile,
        )
    if args.command == CliCommand.DOCTOR:
        return handlers.run_doctor(database=invocation.database, client=client)
    if args.command == CliCommand.JANITOR:
        return handlers.run_janitor(
            database=invocation.database,
            retention_days=args.retention_days,
            minimum_rollback_deployments=args.minimum_rollback_deployments,
            apply=bool(getattr(args, "apply", False)),
            json_output=bool(getattr(args, "json", False)),
            client=client,
        )
    if args.command == CliCommand.REPAIR:
        return handlers.run_repair_active_view(
            database=invocation.database,
            table=args.table,
            deployment_id=args.deployment_id,
            client=client,
        )
    return handlers.run_plan(
        options=PlanCommandOptions(
            pipelines_root=_require_pipelines_root(invocation),
            database=invocation.database,
            selectors=tuple(getattr(args, "select", [])),
            full_refresh=bool(getattr(args, "full_refresh", False)),
            start_time=getattr(args, "start_time", None),
            deployment_id=getattr(args, "deployment_id", None),
            json_output=bool(getattr(args, "json", False)),
            verbose=bool(getattr(args, "verbose", False)),
            changed=bool(getattr(args, "changed", False)),
            include_missing_upstream=bool(getattr(args, "include_missing_upstream", False)),
        ),
        client=client,
        loaded_project=invocation.loaded_project,
        adapter_profile=adapter_profile,
    )


def _resolve_connection(
    *,
    adapter_connection: AdapterConnection | None,
) -> AdapterConnection:
    if adapter_connection is None:
        raise CliUserError("Command requires a resolved adapter connection")
    return adapter_connection


def _require_pipelines_root(invocation: ResolvedCliInvocation) -> Path:
    if invocation.pipelines_root is None:
        raise CliUserError("Command requires a resolved pipelines root")
    return invocation.pipelines_root
