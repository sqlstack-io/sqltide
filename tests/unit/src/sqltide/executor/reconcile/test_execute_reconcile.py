import json
from hashlib import sha256
from typing import cast

import pytest

from sqltide._native import execute_reconcile
from sqltide.compiler.compile.models import DesiredState
from sqltide.compiler.planner.models import ActualState, ObjectStateRecord
from sqltide.executor.reconcile.models import ReconcilePreview, ReconcileResult
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.reconcile._test_types import (
    ApplyReconcileWorkflowTestCase,
    ExecuteReconcileTestCase,
)
from tests.unit.src.sqltide.executor.reconcile.helpers import (
    ReconcileWorkflowAdapterConnection,
    build_matching_reconcile_states,
    build_misdirected_reconcile_states,
    build_structurally_mismatched_reconcile_states,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ExecuteReconcileTestCase(
            description="builds records for a structurally matching target and materialized view",
            build_states=build_matching_reconcile_states,
            expected_eligible_names=("tbl__orders", "mv__orders"),
            expected_rejected_reason_groups=(),
            expected_reconcile_id_prefix="reconcile_",
        ),
        ExecuteReconcileTestCase(
            description="rejects an engine mismatch and missing materialized view",
            build_states=build_structurally_mismatched_reconcile_states,
            expected_eligible_names=(),
            expected_rejected_reason_groups=(
                (
                    "engine does not match",
                    "live transform materialized view not found",
                ),
            ),
            expected_reconcile_id_prefix="reconcile_",
        ),
        ExecuteReconcileTestCase(
            description="rejects a materialized view wired to different relations",
            build_states=build_misdirected_reconcile_states,
            expected_eligible_names=(),
            expected_rejected_reason_groups=(
                (
                    "live transform source does not match",
                    "live transform target does not match",
                ),
            ),
            expected_reconcile_id_prefix="reconcile_",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_desired_and_actual_states_when_reconciling_then_classifies_targets(
    test_case: ExecuteReconcileTestCase,
) -> None:
    desired_state: DesiredState
    actual_state: ActualState
    desired_state, actual_state = test_case.build_states()
    connection: RecordingAdapterConnection = RecordingAdapterConnection()

    result: ReconcilePreview = cast(
        ReconcilePreview,
        execute_reconcile(
            client=connection,
            target_database="analytics",
            metadata_database="metadata",
            desired_state=desired_state,
            actual_state=actual_state,
            selected_model_keys=frozenset(),
        ),
    )

    assert (
        tuple(record.key.name for record in result.eligible_records)
        == test_case.expected_eligible_names
    )
    assert (
        tuple(rejected.reasons for rejected in result.rejected_targets)
        == test_case.expected_rejected_reason_groups
    )
    assert result.reconcile_id.startswith(test_case.expected_reconcile_id_prefix)
    assert connection.target_mutation_lock_events == []


@pytest.mark.parametrize(
    "test_case",
    [
        ApplyReconcileWorkflowTestCase(
            description="executes metadata migration and exact reconcile persistence SQL",
            expected_lock_database="analytics",
            expected_migration_statement="CREATE DATABASE IF NOT EXISTS metadata;",
            expected_object_names=("tbl__orders", "mv__orders"),
            expected_reconcile_id_prefix="reconcile_",
            expected_table_fingerprint=(
                '{"columns": [{"default": null, "name": "order_id", "type": "String"}], '
                '"storage": {"engine": "MergeTree()", "order_by": ["order_id"], '
                '"partition_by": null, "settings": null, "ttl": null}}'
            ),
            expected_view_fingerprint=(
                '{"database_template": null, "query": "SELECT order_id FROM raw__orders", '
                '"source_table_name": "raw__orders", "target_table_name": "tbl__orders"}'
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_matching_state_when_applying_reconcile_then_exact_workflow_sql_reaches_gateway(
    test_case: ApplyReconcileWorkflowTestCase,
) -> None:
    desired_state: DesiredState
    actual_state: ActualState
    desired_state, actual_state = build_matching_reconcile_states()
    connection: ReconcileWorkflowAdapterConnection = ReconcileWorkflowAdapterConnection()

    result: ReconcileResult = cast(
        ReconcileResult,
        execute_reconcile(
            client=connection,
            target_database=test_case.expected_lock_database,
            metadata_database="metadata",
            desired_state=desired_state,
            actual_state=actual_state,
            selected_model_keys=frozenset(),
            apply=True,
        ),
    )
    table_record: ObjectStateRecord = result.reconciled_records[0]
    view_record: ObjectStateRecord = result.reconciled_records[1]
    table_observation_id: str = sha256(
        json.dumps(
            {
                "state_id": result.reconcile_id,
                "state_kind": "reconcile",
                "logical_database_name": None,
                "logical_object_type": "table",
                "logical_object_name": "tbl__orders",
                "physical_database_name": None,
                "physical_relation_name": "tbl__orders",
                "object_fingerprint": test_case.expected_table_fingerprint,
                "canonical_query": None,
                "observed_at": table_record.recorded_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    view_observation_id: str = sha256(
        json.dumps(
            {
                "state_id": result.reconcile_id,
                "state_kind": "reconcile",
                "logical_database_name": None,
                "logical_object_type": "materialized_view",
                "logical_object_name": "mv__orders",
                "physical_database_name": None,
                "physical_relation_name": "mv__orders",
                "object_fingerprint": test_case.expected_view_fingerprint,
                "canonical_query": "SELECT order_id FROM raw__orders",
                "observed_at": view_record.recorded_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected_persistence_statement: str = (
        "INSERT INTO metadata._sqltide_virtual_object_state "
        "(state_id, observation_id, state_kind, deployment_id, logical_database_name, "
        "logical_object_type, "
        "logical_object_name, physical_database_name, physical_relation_name, "
        "logical_model_database, logical_model_name, is_selected_root, object_fingerprint, "
        "canonical_query, observed_at) VALUES\n"
        f"('{result.reconcile_id}', '{table_observation_id}', 'reconcile', NULL, NULL, "
        "'table', 'tbl__orders', NULL, "
        f"'tbl__orders', NULL, 'tbl__orders', false, "
        f"'{test_case.expected_table_fingerprint}', NULL, '{table_record.recorded_at}'),\n"
        f"('{result.reconcile_id}', '{view_observation_id}', 'reconcile', NULL, NULL, "
        "'materialized_view', "
        f"'mv__orders', NULL, 'mv__orders', NULL, 'mv__orders', false, "
        f"'{test_case.expected_view_fingerprint}', 'SELECT order_id FROM raw__orders', "
        f"'{view_record.recorded_at}');"
    )

    assert connection.statements == [
        test_case.expected_migration_statement,
        expected_persistence_statement,
    ]
    assert tuple(event[:2] for event in connection.target_mutation_lock_events) == (
        ("acquire", test_case.expected_lock_database),
        ("release", test_case.expected_lock_database),
    )
    assert tuple(record.key.name for record in result.reconciled_records) == (
        test_case.expected_object_names
    )
    assert tuple(record.deployment_id for record in result.reconciled_records) == (
        result.reconcile_id,
        result.reconcile_id,
    )
    assert result.reconcile_id.startswith(test_case.expected_reconcile_id_prefix)
