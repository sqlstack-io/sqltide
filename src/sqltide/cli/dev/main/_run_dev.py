"""`sqt dev` command handler."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import sqltide._native as _native
from sqltide._native import analyze_project
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.auth.main.default_control_store_url import default_control_store_url
from sqltide.auth.models import AuthSettings
from sqltide.auth.types import AuthenticationMode
from sqltide.cli.dev.classes.dev_terminal_reporter import DevTerminalReporter
from sqltide.cli.dev.models import DevCommandOptions
from sqltide.cli.presentation.main._cli_style import cli_style
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import (
    EffectiveProjectConfiguration,
    LoadedProject,
    ProjectDefaults,
)
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.main.run_dev_server import run_dev_server
from sqltide.dev_server.models import DevExecutionContext


def run_dev(
    *,
    options: DevCommandOptions,
    client: AdapterConnection | None,
    observation_client: AdapterConnection | None = None,
    connection_factory: Callable[[], AdapterConnection] | None = None,
    observation_connection_factory: Callable[[], AdapterConnection] | None = None,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Serve the dev UI and API over this project until interrupted."""

    project_dir: Path = options.pipelines_root.parent
    effective_configuration: EffectiveProjectConfiguration | None = getattr(
        loaded_project, "effective_configuration", None
    )
    project_defaults: ProjectDefaults = (
        effective_configuration.defaults
        if effective_configuration is not None
        else ProjectDefaults()
    )
    execution_context: DevExecutionContext = DevExecutionContext(
        database=options.database,
        selected_target=options.selected_target,
        cli_variables=options.cli_variables,
        environment=options.environment,
        connection_host=options.connection_host,
        connection_port=options.connection_port,
        connection_username=options.connection_username,
        connection_password=options.connection_password,
        run_presumed_failed_after_seconds=project_defaults.run_presumed_failed_after_seconds,
        connection_factory=connection_factory,
        observation_connection_factory=observation_connection_factory,
    )

    def run_compile() -> CompileAnalysis:
        reloaded: LoadedProject | None = (
            _native.load_project_input_for_path(
                path=project_dir,
                selected_target=execution_context.selected_target,
                cli_variables=dict(execution_context.cli_variables),
                environment=execution_context.environment,
            )
            if loaded_project is not None
            else None
        )
        return analyze_project(
            pipelines_root=options.pipelines_root,
            loaded_project=reloaded,
            adapter_profile=adapter_profile,
        )

    return run_dev_server(
        run_compile=run_compile,
        connection=client,
        observation_connection=observation_client,
        database=options.database,
        project_dir=project_dir,
        host=options.host,
        port=options.port,
        reporter=DevTerminalReporter(style=cli_style()),
        execution_context=execution_context,
        auth_settings=options.auth_settings
        or AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url=default_control_store_url(project_dir=project_dir),
        ),
    )
