from typing import cast

import pytest

from sqltide._native import execute_direct_reconcile
from sqltide.adapter.models import CatalogSnapshot
from sqltide.compiler.compile.models import CompiledModel, DesiredState, ObjectKey
from sqltide.executor.reconcile.models import ReconcilePreview, ReconcileResult
from tests.unit.src.sqltide.compiler.graph.helpers import build_typed_graph_project
from tests.unit.src.sqltide.executor.reconcile._test_types import DirectReconcileTestCase
from tests.unit.src.sqltide.executor.reconcile.helpers import (
    DirectReconcileWorkflowAdapterConnection,
    build_matching_direct_reconcile_state,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectReconcileTestCase(
            description="matching direct table and transform are eligible",
            expected_target_names=("tbl__orders", "mv__orders"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_matching_direct_state_when_previewing_then_target_is_eligible(
    test_case: DirectReconcileTestCase,
) -> None:
    desired_state: DesiredState
    catalog: CatalogSnapshot
    target_key: ObjectKey
    desired_state, catalog, target_key = build_matching_direct_reconcile_state()
    connection: DirectReconcileWorkflowAdapterConnection = (
        DirectReconcileWorkflowAdapterConnection()
    )

    result: ReconcilePreview = cast(
        ReconcilePreview,
        execute_direct_reconcile(
            client=connection,
            target_database="analytics",
            metadata_database="metadata",
            desired_state=desired_state,
            catalog=catalog,
            models=(),
            selected_model_keys=frozenset({target_key}),
            tool_version="test",
        ),
    )

    assert tuple(record.key.name for record in result.eligible_records) == (
        test_case.expected_target_names
    )
    assert result.rejected_targets == ()
    assert connection.target_mutation_lock_events == []


@pytest.mark.parametrize(
    "test_case",
    [
        DirectReconcileTestCase(
            description="matching direct state persists one logical model baseline",
            expected_target_names=("tbl__orders", "mv__orders"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_matching_direct_state_when_applying_then_records_fingerprint(
    test_case: DirectReconcileTestCase,
) -> None:
    desired_state: DesiredState
    catalog: CatalogSnapshot
    target_key: ObjectKey
    desired_state, catalog, target_key = build_matching_direct_reconcile_state()
    model: CompiledModel = build_typed_graph_project().models[0]
    connection: DirectReconcileWorkflowAdapterConnection = (
        DirectReconcileWorkflowAdapterConnection()
    )

    result: ReconcileResult = cast(
        ReconcileResult,
        execute_direct_reconcile(
            client=connection,
            target_database="analytics",
            metadata_database="metadata",
            desired_state=desired_state,
            catalog=catalog,
            models=(model,),
            selected_model_keys=frozenset({target_key}),
            tool_version="test",
            apply=True,
        ),
    )

    assert connection.direct_fingerprint_databases == ["metadata"]
    assert len(connection.direct_fingerprint_records) == 1
    assert connection.direct_fingerprint_records[0].logical_model_identity == "analytics.enriched"
    assert tuple(event[:2] for event in connection.target_mutation_lock_events) == (
        ("acquire", "analytics"),
        ("release", "analytics"),
    )
    assert tuple(record.key.name for record in result.reconciled_records) == (
        test_case.expected_target_names
    )
