"""Resolve connection ownership for one CLI invocation."""

from collections.abc import Callable, Mapping
from functools import partial

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterConfigurationError
from sqltide.adapter.models import AdapterConnectionConfig
from sqltide.cli.entry._helpers.entrypoint import resolve_adapter_connection_config
from sqltide.cli.entry.constants import COMMANDS_WITHOUT_ADAPTER_CONNECTION
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.models import (
    ResolvedCliInvocation,
    ResolvedInvocationConnection,
)
from sqltide.cli.entry.types import CliCommand
from sqltide.compiler.discovery.models import LoadedProject


def resolve_invocation_connection(
    *,
    invocation: ResolvedCliInvocation,
    provided_connection: AdapterConnection | None,
) -> ResolvedInvocationConnection:
    """Resolve a borrowed or CLI-owned connection for one invocation."""

    if invocation.args.command in COMMANDS_WITHOUT_ADAPTER_CONNECTION:
        return ResolvedInvocationConnection(connection=None, close_after_command=False)
    if provided_connection is not None:
        return ResolvedInvocationConnection(
            connection=provided_connection,
            close_after_command=False,
        )
    config: AdapterConnectionConfig = resolve_invocation_connection_config(invocation)
    return ResolvedInvocationConnection(
        connection=_connect(invocation=invocation, config=config),
        close_after_command=True,
    )


def resolve_primary_connection(
    *,
    invocation: ResolvedCliInvocation,
    provided_connection: AdapterConnection | None,
    resolver: Callable[..., ResolvedInvocationConnection] = resolve_invocation_connection,
) -> tuple[
    ResolvedInvocationConnection,
    Callable[[], AdapterConnection] | None,
    Callable[[], AdapterConnection] | None,
]:
    """Defer dev connections while preserving eager ownership for finite commands."""

    if invocation.args.command != CliCommand.DEV or provided_connection is not None:
        return (
            resolver(invocation=invocation, provided_connection=provided_connection),
            None,
            None,
        )
    config: AdapterConnectionConfig = resolve_invocation_connection_config(invocation)
    factory: Callable[[], AdapterConnection] = partial(
        _connect, invocation=invocation, config=config
    )
    return (
        ResolvedInvocationConnection(connection=None, close_after_command=False),
        factory,
        factory,
    )


def resolve_observation_connection(
    *,
    invocation: ResolvedCliInvocation,
    provided_primary_connection: AdapterConnection | None,
    provided_observation_connection: AdapterConnection | None,
    resolver: Callable[..., ResolvedInvocationConnection] = resolve_invocation_connection,
) -> ResolvedInvocationConnection | None:
    """Resolve the separate connection required by finite observed mutations."""

    command: CliCommand = CliCommand(invocation.args.command)
    observed_commands: frozenset[CliCommand] = frozenset(
        {CliCommand.BUILD, CliCommand.DESTROY, CliCommand.RESET_TARGET}
    )
    if provided_primary_connection is None and command in observed_commands:
        return resolver(invocation=invocation, provided_connection=None)
    if provided_primary_connection is not None and command in observed_commands:
        if provided_observation_connection is None:
            display: str = "Build" if command == CliCommand.BUILD else "Destruction"
            raise CliUserError(f"{display} requires a dedicated observation connection")
        return ResolvedInvocationConnection(
            connection=provided_observation_connection,
            close_after_command=False,
        )
    if provided_primary_connection is not None and command == CliCommand.DEV:
        return ResolvedInvocationConnection(
            connection=provided_observation_connection,
            close_after_command=False,
        )
    return None


def _connect(
    *, invocation: ResolvedCliInvocation, config: AdapterConnectionConfig
) -> AdapterConnection:
    connection: AdapterConnection = invocation.adapter.connect(config)
    metadata_databases: set[str | None] = {
        invocation.database,
        getattr(invocation.args, "metadata_database", None),
    }
    try:
        for database in sorted(name for name in metadata_databases if name is not None):
            connection.rename_streambuild_metadata(database)
    except BaseException:
        connection.close()
        raise
    return connection


def resolve_invocation_connection_config(
    invocation: ResolvedCliInvocation,
) -> AdapterConnectionConfig:
    """Resolve adapter-owned connection settings without opening a connection."""
    if invocation.connection.raw_project_connection is None:
        return resolve_adapter_connection_config(
            host=invocation.connection.host,
            port=invocation.connection.port,
            username=invocation.connection.username,
            password=invocation.connection.password,
            project_connection=invocation.connection.project_connection,
        )
    raw_values: dict[str, object] = dict(invocation.connection.raw_project_connection.values)
    cli_values: dict[str, object | None] = {
        "host": invocation.connection.host,
        "port": invocation.connection.port,
        "username": invocation.connection.username,
        "password": invocation.connection.password,
    }
    key: str
    value: object | None
    for key, value in cli_values.items():
        if value is not None:
            raw_values[key] = value
    environment: Mapping[str, str] = invocation.connection.environment or {}
    variables: dict[str, object] = dict(invocation.connection.variables)
    expanded_values: dict[str, object] = {
        name: _native.interpolate_config_value(
            value=raw_value,
            variables=variables,
            environment=environment,
            field_path=f"connection.{name}",
        )
        for name, raw_value in raw_values.items()
    }
    try:
        connection_database: str | None = (
            None if CliCommand(invocation.args.command) == CliCommand.BUILD else invocation.database
        )
        return invocation.adapter.build_connection_config(
            values=expanded_values,
            database=connection_database,
        )
    except AdapterConfigurationError as error:
        loaded_project: LoadedProject | None = invocation.loaded_project
        if loaded_project is None or loaded_project.configuration is None:
            raise
        project_sources: list[str] = [str(loaded_project.configuration.project_source.file_path)]
        if loaded_project.configuration.local_source is not None:
            project_sources.append(str(loaded_project.configuration.local_source.file_path))
        raise AdapterConfigurationError(
            f"Effective connection from {', '.join(project_sources)} is invalid: {error}"
        ) from error
