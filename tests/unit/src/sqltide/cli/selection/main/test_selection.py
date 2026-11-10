from __future__ import annotations

from dataclasses import replace

import pytest

from sqltide._native import build_project_graph_from_compiled_project
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.selection.main._selection import resolve_selection
from sqltide.cli.selection.models import SelectionResolution
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.types import ReplayLineageMode
from sqltide.compiler.pipeline.models import RealizedProject
from tests.unit.src.sqltide.cli.selection.main._test_types import (
    CliExecutionClosureLineageMismatchTestCase,
    CliSelectionDesiredStateIdentityTestCase,
    CliSelectionLineageMismatchTestCase,
    CliSelectionResolutionErrorTestCase,
    CliSelectionResolutionTestCase,
)
from tests.unit.src.sqltide.cli.selection.main.helpers import (
    compile_selector_project,
    realize_cross_pipeline_reference_project,
    realize_selector_project,
    realize_view_selector_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectionResolutionTestCase(
            description="bare model selector includes downstream closure and upstream deps",
            selectors=("orders_clean",),
            expected_selected_logical_model_names=("orders_clean",),
            expected_selected_model_names=("tbl__orders_clean",),
            expected_object_names=(
                "kafka__orders",
                "raw__orders",
                "mv__orders",
                "tbl__orders_clean",
                "mv__orders_clean",
                "tbl__orders_enriched",
                "mv__orders_enriched",
            ),
        ),
        CliSelectionResolutionTestCase(
            description="pipeline selector includes all authored models in one pipeline only",
            selectors=("pipeline:pl__payments",),
            expected_selected_logical_model_names=("payments_enriched",),
            expected_selected_model_names=("tbl__payments_enriched",),
            expected_object_names=(
                "kafka__payments",
                "raw__payments",
                "mv__payments",
                "tbl__payments_enriched",
                "mv__payments_enriched",
            ),
        ),
        CliSelectionResolutionTestCase(
            description="bare pipeline name resolves globally without the pipeline: prefix",
            selectors=("pl__payments",),
            expected_selected_logical_model_names=("payments_enriched",),
            expected_selected_model_names=("tbl__payments_enriched",),
            expected_object_names=(
                "kafka__payments",
                "raw__payments",
                "mv__payments",
                "tbl__payments_enriched",
                "mv__payments_enriched",
            ),
        ),
        CliSelectionResolutionTestCase(
            description="model: prefix is optional sugar equivalent to the bare model name",
            selectors=("model:orders_clean",),
            expected_selected_logical_model_names=("orders_clean",),
            expected_selected_model_names=("tbl__orders_clean",),
            expected_object_names=(
                "kafka__orders",
                "raw__orders",
                "mv__orders",
                "tbl__orders_clean",
                "mv__orders_clean",
                "tbl__orders_enriched",
                "mv__orders_enriched",
            ),
        ),
        CliSelectionResolutionTestCase(
            description="one selector value holding whitespace-separated names unions them",
            selectors=("orders_clean pl__payments",),
            expected_selected_logical_model_names=("orders_clean", "payments_enriched"),
            expected_selected_model_names=("tbl__orders_clean", "tbl__payments_enriched"),
            expected_object_names=(
                "kafka__orders",
                "raw__orders",
                "mv__orders",
                "tbl__orders_clean",
                "mv__orders_clean",
                "tbl__orders_enriched",
                "mv__orders_enriched",
                "kafka__payments",
                "raw__payments",
                "mv__payments",
                "tbl__payments_enriched",
                "mv__payments_enriched",
            ),
        ),
        CliSelectionResolutionTestCase(
            description="multiple selectors union before closure expansion",
            selectors=("orders_clean", "pipeline:pl__payments"),
            expected_selected_logical_model_names=("orders_clean", "payments_enriched"),
            expected_selected_model_names=("tbl__orders_clean", "tbl__payments_enriched"),
            expected_object_names=(
                "kafka__orders",
                "raw__orders",
                "mv__orders",
                "tbl__orders_clean",
                "mv__orders_clean",
                "tbl__orders_enriched",
                "mv__orders_enriched",
                "kafka__payments",
                "raw__payments",
                "mv__payments",
                "tbl__payments_enriched",
                "mv__payments_enriched",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_selectors_when_resolving_then_it_returns_expected_filtered_desired_state(
    test_case: CliSelectionResolutionTestCase,
) -> None:
    realized_project: RealizedProject = compile_selector_project()

    resolution: SelectionResolution = resolve_selection(
        realized_project=realized_project,
        graph=build_project_graph_from_compiled_project(project=realized_project.project),
        selectors=test_case.selectors,
    )

    assert tuple(sorted(key.name for key in resolution.selected_model_keys)) == tuple(
        sorted(test_case.expected_selected_model_names)
    )
    assert tuple(sorted(key.name for key in resolution.selected_logical_model_keys)) == tuple(
        sorted(test_case.expected_selected_logical_model_names)
    )
    assert tuple(sorted(object_.name for object_ in resolution.desired_state.objects)) == tuple(
        sorted(test_case.expected_object_names)
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectionResolutionTestCase(
            description="view selector uses the ordinary view as its primary desired key",
            selectors=("customer_orders",),
            expected_selected_logical_model_names=("customer_orders",),
            expected_selected_model_names=("customer_orders",),
            expected_object_names=("customer_orders",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_view_only_project_when_resolving_then_selection_requires_no_replay_mode(
    test_case: CliSelectionResolutionTestCase,
) -> None:
    realized_project: RealizedProject = realize_view_selector_project()

    resolution: SelectionResolution = resolve_selection(
        realized_project=realized_project,
        graph=build_project_graph_from_compiled_project(project=realized_project.project),
        selectors=test_case.selectors,
    )

    assert tuple(key.name for key in resolution.selected_model_keys) == (
        test_case.expected_selected_model_names
    )
    assert tuple(object_.name for object_ in resolution.desired_state.objects) == (
        test_case.expected_object_names
    )
    assert resolution.replay_lineage_mode is None


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectionDesiredStateIdentityTestCase(
            description="unfiltered selection retains the lifecycle desired-state artifact",
            expected_same_identity=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unfiltered_compiled_project_when_resolving_then_reuses_desired_state(
    test_case: CliSelectionDesiredStateIdentityTestCase,
) -> None:
    realized_project: RealizedProject = compile_selector_project()

    resolution: SelectionResolution = resolve_selection(
        realized_project=realized_project,
        graph=build_project_graph_from_compiled_project(project=realized_project.project),
        selectors=(),
    )

    assert (
        resolution.desired_state is realized_project.desired_state
    ) is test_case.expected_same_identity


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectionResolutionErrorTestCase(
            description="plus syntax is rejected clearly",
            selectors=("+orders_clean",),
            expected_error_fragment="\\+.*is not supported",
        ),
        CliSelectionResolutionErrorTestCase(
            description="unknown selector namespace is rejected clearly",
            selectors=("tag:finance",),
            expected_error_fragment="Unsupported selector namespace 'tag'",
        ),
        CliSelectionResolutionErrorTestCase(
            description="unknown bare name is rejected instead of silently ignored",
            selectors=("not_a_real_node",),
            expected_error_fragment="Unknown selected model or pipeline 'not_a_real_node'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_selectors_when_resolving_then_it_raises_clear_error(
    test_case: CliSelectionResolutionErrorTestCase,
) -> None:
    realized_project: RealizedProject = compile_selector_project()

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        resolve_selection(
            realized_project=realized_project,
            graph=build_project_graph_from_compiled_project(project=realized_project.project),
            selectors=test_case.selectors,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        CliSelectionLineageMismatchTestCase(
            description="replay lineage mismatch error names conflicting pipelines and modes",
            selectors=(),
            mutated_pipeline_name="pl__payments",
            expected_error_fragment=(
                "Selected pipelines disagree on replay_lineage_mode: "
                "pl__orders=offsets, pl__payments=timestamp"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_conflicting_selected_pipeline_modes_when_resolving_then_it_raises_contextual_error(
    test_case: CliSelectionLineageMismatchTestCase,
) -> None:
    realized_project: RealizedProject = compile_selector_project()
    compiled_pipelines: tuple[CompiledPipeline, ...] = realized_project.project.pipelines
    pipeline_names: tuple[str, ...] = tuple(
        compiled_pipeline.pipeline.name for compiled_pipeline in compiled_pipelines
    )
    mutated_pipeline_index: int = pipeline_names.index(test_case.mutated_pipeline_name)
    mutated_pipeline: CompiledPipeline = replace(
        compiled_pipelines[mutated_pipeline_index],
        effective_replay_lineage_mode=ReplayLineageMode.TIMESTAMP,
    )
    mutated_compiled_pipelines: tuple[CompiledPipeline, ...] = (
        *compiled_pipelines[:mutated_pipeline_index],
        mutated_pipeline,
        *compiled_pipelines[mutated_pipeline_index + 1 :],
    )

    mutated_realized_project: RealizedProject = realize_selector_project(mutated_compiled_pipelines)

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        resolve_selection(
            realized_project=mutated_realized_project,
            graph=build_project_graph_from_compiled_project(
                project=mutated_realized_project.project
            ),
            selectors=test_case.selectors,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        CliExecutionClosureLineageMismatchTestCase(
            description="rejects replay modes that conflict only after side-ref closure",
            selectors=("payments_enriched",),
            expected_error_fragment=(
                "Selected pipelines disagree on replay_lineage_mode: "
                "pl__orders=timestamp, pl__payments=offsets"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_side_ref_expands_to_conflicting_mode_when_resolving_then_rejects_closure(
    test_case: CliExecutionClosureLineageMismatchTestCase,
) -> None:
    realized_project: RealizedProject = realize_cross_pipeline_reference_project()

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        resolve_selection(
            realized_project=realized_project,
            graph=build_project_graph_from_compiled_project(project=realized_project.project),
            selectors=test_case.selectors,
        )
