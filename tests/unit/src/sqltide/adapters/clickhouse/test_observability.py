import pytest

from sqltide.adapter.models import (
    AdapterCurrentQualityNode,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
)
from sqltide.adapters.clickhouse._helpers.metadata import (
    render_clickhouse_latest_node_status_query,
    render_clickhouse_run_event_inserts,
    render_clickhouse_run_statement_inserts,
)
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    LatestNodeStatusQueryTestCase,
    RunEventInsertsTestCase,
    RunStatementInsertsTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        LatestNodeStatusQueryTestCase(
            description="classifies current identity drift and never-run history",
            expected_current_status_fragment=(
                "matching.node_name != '', matching.status, "
                "latest.binding_key != manifest.binding_key, 'binding_changed'"
            ),
            expected_node_values_fragment=(
                "('audit', 'order ids are present', 'binding', "
                "'current-fingerprint', 'execution', NULL, 0)"
            ),
            expected_target_fragment=("WHERE result.target_identity = 'analytics'"),
            expected_project_fragment=(
                "invocation.project_identity = 'current' OR "
                "endsWith(invocation.project_identity, '/current')"
            ),
            expected_logical_slot_fragment=(
                "GROUP BY result.node_kind, result.node_name, result.binding_key, "
                "result.execution_fingerprint, ifNull(toString(result.scheduled_for), "
                "result.result_id)"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_current_manifest_nodes_when_rendering_status_query_then_all_ui_states_are_explicit(
    test_case: LatestNodeStatusQueryTestCase,
) -> None:
    query: str = render_clickhouse_latest_node_status_query(
        database="metadata",
        project_identity="/project/current",
        target_identity="analytics",
        nodes=(
            AdapterCurrentQualityNode(
                node_kind="audit",
                node_name="order ids are present",
                binding_key="binding",
                definition_fingerprint="current-fingerprint",
                execution_fingerprint="execution",
            ),
        ),
    )

    assert test_case.expected_current_status_fragment in query
    assert test_case.expected_node_values_fragment in query
    assert test_case.expected_target_fragment in query
    assert test_case.expected_project_fragment in query
    assert test_case.expected_logical_slot_fragment in query


@pytest.mark.parametrize(
    "test_case",
    [
        RunEventInsertsTestCase(
            description="renders one bare insert for a mid-run event",
            include_migration=False,
            expected_statement_count=1,
            expected_insert_fragment=(
                "INSERT INTO metadata._sqltide_run_events "
                "(invocation_id, sequence, event_kind, step_id, phase, payload_json)"
            ),
            expected_values_fragment=(
                "('inv-1', 3, 'statement_completed', "
                "'replay_orders', 'replay', '{\"writtenRows\": 42}')"
            ),
        ),
        RunEventInsertsTestCase(
            description="prepends the idempotent migration for the first event",
            include_migration=True,
            expected_statement_count=22,
            expected_insert_fragment="CREATE DATABASE IF NOT EXISTS metadata;",
            expected_values_fragment=("emitted_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_run_event_when_rendering_inserts_then_sql_is_exact(
    test_case: RunEventInsertsTestCase,
) -> None:
    rendered: tuple[str, ...] = render_clickhouse_run_event_inserts(
        database="metadata",
        events=(
            AdapterRunEventRecord(
                invocation_id="inv-1",
                sequence=3,
                event_kind="statement_completed",
                step_id="replay_orders",
                phase="replay",
                payload_json='{"writtenRows": 42}',
            ),
        ),
        include_migration=test_case.include_migration,
    )

    assert len(rendered) == test_case.expected_statement_count
    assert test_case.expected_insert_fragment in rendered[0]
    assert any(test_case.expected_values_fragment in statement for statement in rendered)


@pytest.mark.parametrize(
    "test_case",
    [
        RunStatementInsertsTestCase(
            description="renders exact run statement sql hashes and revision",
            expected_statement_count=1,
            expected_fragments=(
                "INSERT INTO metadata._sqltide_run_statements",
                "INSERT INTO orders SELECT 1;",
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
                "workflow_revision",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_run_statements_when_rendering_then_exact_sql_and_hashes_are_inserted(
    test_case: RunStatementInsertsTestCase,
) -> None:
    rendered: tuple[str, ...] = render_clickhouse_run_statement_inserts(
        database="metadata",
        statements=(
            AdapterRunStatementRecord(
                invocation_id="inv-1",
                statement_sequence=3,
                step_id="replay_orders",
                phase="replay",
                intent="mutation",
                sql="INSERT INTO orders SELECT 1;",
                sql_sha256="a" * 64,
                workflow_sha256="b" * 64,
                workflow_revision=2,
            ),
        ),
    )

    assert len(rendered) == test_case.expected_statement_count
    assert all(fragment in rendered[0] for fragment in test_case.expected_fragments)
