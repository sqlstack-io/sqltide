"""CLI command for mode-aware builds."""

import os
import sys
from time import monotonic_ns

from sqltide._native import (
    analyze_project,
    prepare_validated_build,
    render_interrupted_build_message,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.adapter.models import AdapterInvocationRecord
from sqltide.cli.build._helpers.direct_command import execute_direct_build_command
from sqltide.cli.build._helpers.virtual_command import execute_virtual_build_command
from sqltide.cli.build.exceptions import BuildPipelineLimitError
from sqltide.cli.build.main._execute_mixed_build import execute_mixed_build_command
from sqltide.cli.build.main._initialize_build_metadata import (
    initialize_build_metadata,
)
from sqltide.cli.build.models import (
    BuildCommandOptions,
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.entry.main._resolve_default_database import resolve_default_database
from sqltide.compiler.compile.exceptions import TransformSqlContractError
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.direct.exceptions import DirectBuildError
from sqltide.executor.observability.constants import RUN_INVOCATION_ID_ENV_VAR
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import RunStartupTimings, TerminalInvocation
from sqltide.executor.workflow.exceptions import WorkflowExecutionError


def run_build(
    *,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Plan and execute one build through the effective project mode."""

    started: tuple[str, str, int] = start_invocation(
        invocation_id=os.environ.get(RUN_INVOCATION_ID_ENV_VAR)
    )
    resolved_database: str = options.database or ""
    try:
        if options.json_output and not options.auto_approve:
            _persist_failed_build(
                started=started,
                options=options,
                client=client,
                error_message="--json requires --auto-approve for build",
            )
            print("--json requires --auto-approve for build", file=sys.stderr)
            return 1
        if options.events_output and not options.auto_approve:
            _persist_failed_build(
                started=started,
                options=options,
                client=client,
                error_message="--events requires --auto-approve for build",
            )
            print("--events requires --auto-approve for build", file=sys.stderr)
            return 1
        compile_started_ns: int = monotonic_ns()
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=options.pipelines_root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
        compile_ms: int = (monotonic_ns() - compile_started_ns) // 1_000_000
        database: str = resolve_default_database(
            loaded_pipelines=list(analysis.compile_inputs.pipelines),
            override=options.database,
        )
        resolved_database = database
        observability_started_ns: int = monotonic_ns()
        initialize_build_metadata(
            analysis=analysis,
            invocation_id=started[0],
            target_database=database,
            metadata_database=options.metadata_database or database,
            connection=observation_client,
        )
        observability_ms: int = (monotonic_ns() - observability_started_ns) // 1_000_000
        preparation_options: WorkflowPreparationOptions = WorkflowPreparationOptions(
            database=options.database,
            metadata_database=options.metadata_database,
            selectors=options.selectors,
            deployment_id=options.deployment_id,
            full_refresh=options.full_refresh,
            start_time=options.start_time,
            verbose=options.verbose,
            changed=options.changed,
            include_missing_upstream=options.include_missing_upstream,
        )
        planning_started_ns: int = monotonic_ns()
        preparation: (
            DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation
        ) = prepare_validated_build(
            analysis=analysis,
            options=preparation_options,
            client=client,
            adapter_profile=adapter_profile,
        )
        planning_ms: int = (monotonic_ns() - planning_started_ns) // 1_000_000
        startup_timings: RunStartupTimings = RunStartupTimings(
            compile_ms=compile_ms,
            observability_ms=observability_ms,
            planning_ms=planning_ms,
        )
        if isinstance(preparation, MixedWorkflowPreparation):
            return execute_mixed_build_command(
                preparation=preparation,
                options=options,
                client=client,
                observation_client=observation_client,
                started=started,
                startup_timings=startup_timings,
            )
        if isinstance(preparation, VirtualWorkflowPreparation):
            return execute_virtual_build_command(
                preparation=preparation,
                options=options,
                client=client,
                observation_client=observation_client,
                started=started,
                startup_timings=startup_timings,
            )
        return execute_direct_build_command(
            preparation=preparation,
            options=options,
            client=client,
            observation_client=observation_client,
            started=started,
            startup_timings=startup_timings,
        )
    except BuildPipelineLimitError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        TransformSqlContractError,
        CliUserError,
        DirectPlanError,
        DirectBuildError,
        BackfillExecutionError,
        WorkflowExecutionError,
        AdapterError,
        OSError,
        ValueError,
    ) as error:
        _persist_failed_build(
            started=started,
            options=options,
            client=client,
            error_message=str(error),
            database=resolved_database,
        )
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _persist_cancelled_build(
            started=started,
            options=options,
            client=client,
            database=resolved_database,
        )
        try:
            print(render_interrupted_build_message(), file=sys.stderr)
        except Exception:
            pass
        return 130


def _persist_failed_build(
    *,
    started: tuple[str, str, int],
    options: BuildCommandOptions,
    client: AdapterConnection,
    error_message: str,
    database: str | None = None,
) -> None:
    target_database: str = options.metadata_database or database or options.database or ""
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=options.pipelines_root.parent,
            target_identity=database or options.database or "",
            command="build",
            mode=None,
            outcome="failed",
            exit_code=1,
            materialized_outcome=None,
            deployment_id=options.deployment_id,
            workflow_id=None,
            selected_node_count=0,
            error_message=error_message,
            summary={},
        ),
    )
    _ = persist_terminal_observations(
        client=client,
        database=target_database,
        invocation=invocation,
        node_results=(),
    )


def _persist_cancelled_build(
    *,
    started: tuple[str, str, int],
    options: BuildCommandOptions,
    client: AdapterConnection,
    database: str,
) -> None:
    target_database: str = options.metadata_database or database
    invocation: AdapterInvocationRecord = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=options.pipelines_root.parent,
            target_identity=database,
            command="build",
            mode=None,
            outcome="cancelled",
            exit_code=130,
            materialized_outcome=None,
            deployment_id=options.deployment_id,
            workflow_id=None,
            selected_node_count=0,
            error_message=None,
            summary={},
        ),
    )
    _ = persist_terminal_observations(
        client=client,
        database=target_database,
        invocation=invocation,
        node_results=(),
    )
