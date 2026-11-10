"""Execute a confirmed mixed build in virtual-then-direct order."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

from sqltide._native import confirm_build
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.cli.build._helpers.direct_command import execute_direct_build_command
from sqltide.cli.build._helpers.virtual_command import execute_virtual_build_command
from sqltide.cli.build.models import BuildCommandOptions, MixedWorkflowPreparation
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import RunStartupTimings
from sqltide.executor.workflow.main.target_mutation_lock import target_mutation_lock


def execute_mixed_build_command(
    *,
    preparation: MixedWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    startup_timings: RunStartupTimings,
) -> int:
    """Confirm once, stage virtual work, then apply direct work."""

    if not confirm_build(
        options=options,
        plan_text=preparation.plan_text,
        protection_requirements=preparation.protection_requirements,
    ):
        print("Build cancelled.")
        return 1

    with target_mutation_lock(
        connection=client,
        database=preparation.virtual.request.default_database,
        ensure_database=True,
    ):
        if options.json_output:
            return _execute_json_build(
                preparation=preparation,
                options=options,
                client=client,
                observation_client=observation_client,
                started=started,
                startup_timings=startup_timings,
            )

        if not options.events_output:
            print("\nPhase 1/2  VIRTUAL - staging deployment")
        virtual_exit_code: int = execute_virtual_build_command(
            preparation=preparation.virtual,
            options=options,
            client=client,
            observation_client=observation_client,
            started=started,
            confirmation_required=False,
            startup_timings=startup_timings,
            _acquire_target_mutation_lock=False,
        )
        if virtual_exit_code:
            if not options.events_output:
                print("Direct phase skipped because virtual staging failed.", file=sys.stderr)
            return virtual_exit_code

        if not options.events_output:
            print("\nPhase 2/2  DIRECT - applying immediately")
        direct_exit_code: int = execute_direct_build_command(
            preparation=preparation.direct,
            options=options,
            client=client,
            observation_client=observation_client,
            started=start_invocation(),
            confirmation_required=False,
            _acquire_target_mutation_lock=False,
        )
        if not options.events_output:
            if direct_exit_code:
                print(
                    "Mixed build failed during the direct phase. The virtual deployment remains "
                    f"staged as {preparation.virtual.preview.deployment_id}.",
                    file=sys.stderr,
                )
            else:
                print(
                    "\nMixed Build Complete\n"
                    "Direct changes are live. Virtual changes remain staged until promoted with:\n"
                    f"  sqt deployment promote {preparation.virtual.preview.deployment_id}"
                )
        return direct_exit_code


def _execute_json_build(
    *,
    preparation: MixedWorkflowPreparation,
    options: BuildCommandOptions,
    client: AdapterConnection,
    observation_client: AdapterConnection,
    started: tuple[str, str, int],
    startup_timings: RunStartupTimings,
) -> int:
    virtual_output: io.StringIO = io.StringIO()
    with redirect_stdout(virtual_output):
        virtual_exit_code: int = execute_virtual_build_command(
            preparation=preparation.virtual,
            options=options,
            client=client,
            observation_client=observation_client,
            started=started,
            confirmation_required=False,
            startup_timings=startup_timings,
            _acquire_target_mutation_lock=False,
        )
    if virtual_exit_code:
        return virtual_exit_code

    direct_output: io.StringIO = io.StringIO()
    with redirect_stdout(direct_output):
        direct_exit_code: int = execute_direct_build_command(
            preparation=preparation.direct,
            options=options,
            client=client,
            observation_client=observation_client,
            started=start_invocation(),
            confirmation_required=False,
            _acquire_target_mutation_lock=False,
        )
    if direct_exit_code:
        return direct_exit_code

    print(
        json.dumps(
            {
                "mode": "mixed",
                "execution_order": ["virtual", "direct"],
                "virtual": json.loads(virtual_output.getvalue()),
                "direct": json.loads(direct_output.getvalue()),
            },
            indent=2,
        )
    )
    return 0
