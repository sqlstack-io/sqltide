"""CLI command for mode-aware execution planning."""

import sys

from sqltide._native import analyze_project
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import AdapterError
from sqltide.cli.build.main.prepare_build_workflow import prepare_build_workflow
from sqltide.cli.build.models import (
    DirectWorkflowPreparation,
    MixedWorkflowPreparation,
    VirtualWorkflowPreparation,
    WorkflowPreparationOptions,
)
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.plan.models import PlanCommandOptions
from sqltide.cli.workflow_artifacts.main._publish_plan_workflow import publish_plan_workflow
from sqltide.compiler.compile.exceptions import TransformSqlContractError
from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.planner.exceptions import DirectPlanError
from sqltide.executor.backfill.exceptions import BackfillExecutionError
from sqltide.executor.direct.exceptions import DirectBuildError


def run_plan(
    *,
    options: PlanCommandOptions,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
) -> int:
    """Plan the effective mode's execution against live warehouse state."""

    try:
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=options.pipelines_root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
        preparation_options: WorkflowPreparationOptions = WorkflowPreparationOptions(
            database=options.database,
            metadata_database=None,
            selectors=options.selectors,
            deployment_id=options.deployment_id,
            full_refresh=options.full_refresh,
            start_time=options.start_time,
            verbose=options.verbose,
            changed=options.changed,
            include_missing_upstream=options.include_missing_upstream,
        )
        preparation: (
            DirectWorkflowPreparation | MixedWorkflowPreparation | VirtualWorkflowPreparation
        ) = prepare_build_workflow(
            analysis=analysis,
            options=preparation_options,
            client=client,
            adapter_profile=adapter_profile,
        )
        if not isinstance(preparation, MixedWorkflowPreparation):
            publish_plan_workflow(
                target_dir=options.pipelines_root.parent / "target",
                workflow=(
                    preparation.workflow.template
                    if isinstance(preparation, DirectWorkflowPreparation)
                    else preparation.workflow
                ),
                is_template=isinstance(preparation, DirectWorkflowPreparation),
            )
        if options.json_output:
            rendered_output: str = (
                preparation.plan_json
                if isinstance(preparation, MixedWorkflowPreparation)
                else preparation.workflow.plan_json
            )
        else:
            rendered_output = preparation.plan_text + "\n"
        print(rendered_output, end="")
    except (
        TransformSqlContractError,
        CliUserError,
        DirectPlanError,
        DirectBuildError,
        BackfillExecutionError,
        AdapterError,
        ValueError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0
