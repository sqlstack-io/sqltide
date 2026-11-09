import pytest
from clickhouse_connect.driver.client import Client

from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import (
    AdapterCapturedReplayRequest,
    AdapterPhysicalRelationMapping,
    AdapterReplayBoundary,
    AdapterReplayColumns,
    AdapterReplayLowerBound,
    AdapterReplayRelations,
    AdapterReplayRequest,
    AdapterReplayWindow,
)
from sqltide.adapter.types import (
    AdapterReplayBoundaryMode,
    AdapterReplayLowerBoundMode,
    AdapterReplaySeedMode,
)
from sqltide.compiler.planner.main.build_adapter_replay_query import (
    build_adapter_replay_query,
)
from tests.integration.src.sqltide.adapters.clickhouse._test_types import (
    ClickHouseOffsetReplayIntegrationTestCase,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        ClickHouseOffsetReplayIntegrationTestCase(
            description="filters physical offset ranges before parsing raw payloads",
            source_rows=(
                ("malformed outside lower bound", 0, 1),
                ("2026-09-07 10:00:00", 0, 2),
                ("2026-09-07 10:01:00", 0, 3),
                ("malformed outside upper bound", 0, 4),
                ("malformed outside partition range", 1, 10),
                ("2026-09-07 11:00:00", 1, 11),
                ("malformed at exclusive upper bound", 1, 12),
            ),
            expected_positions=((0, 2), (0, 3), (1, 11)),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_offset_replay_when_transforming_rows_then_only_physical_ranges_are_read(
    test_case: ClickHouseOffsetReplayIntegrationTestCase,
    managed_clickhouse_client: AdapterConnection,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    clickhouse_client.command(
        f"""
        CREATE TABLE {clickhouse_database}.raw_events
        (
            kafka_value String,
            event_partition Int32,
            event_offset Int64
        )
        ENGINE = MergeTree
        ORDER BY (event_partition, event_offset)
        """
    )
    clickhouse_client.command(
        f"""
        CREATE TABLE {clickhouse_database}.replayed_events
        (
            event_time DateTime,
            _replay_partition Int32,
            _replay_offset Int64
        )
        ENGINE = MergeTree
        ORDER BY (_replay_partition, _replay_offset)
        """
    )
    clickhouse_client.insert(
        f"{clickhouse_database}.raw_events",
        [list(row) for row in test_case.source_rows],
        column_names=["kafka_value", "event_partition", "event_offset"],
    )
    replay: AdapterReplayRequest = AdapterReplayRequest(
        mode=AdapterReplayBoundaryMode.OFFSETS,
        database=clickhouse_database,
        relations=AdapterReplayRelations(
            root="replayed_events",
            source="raw_events",
            anchor="raw_events",
            target="replayed_events",
        ),
        replay_query=build_adapter_replay_query(
            query=(
                "SELECT parseDateTimeBestEffort(source.kafka_value) AS event_time, "
                "CAST(source.event_partition AS Int32) AS _replay_partition, "
                "CAST(source.event_offset AS Int64) AS _replay_offset "
                "FROM raw_events AS source"
            ),
            source_relation_name="raw_events",
            database=clickhouse_database,
            physical_relation_mappings=(
                AdapterPhysicalRelationMapping(
                    logical_name="raw_events",
                    physical_name="raw_events",
                ),
            ),
        ),
        boundaries=(
            AdapterReplayBoundary(
                boundary_key="_replay_partition=0",
                cutoff_value="3",
                cutoff_inclusive=True,
                partition_value="0",
            ),
            AdapterReplayBoundary(
                boundary_key="_replay_partition=1",
                cutoff_value="12",
                cutoff_inclusive=False,
                partition_value="1",
            ),
        ),
        columns=AdapterReplayColumns(
            partition="event_partition",
            offset="event_offset",
            timestamp="event_timestamp",
            landed_at="event_landed_at",
            cursor="event_cursor",
        ),
        window=AdapterReplayWindow(
            lower_bound_mode=AdapterReplayLowerBoundMode.ACTIVE_FRONTIER,
            lower_bound_inclusive=True,
            boundary_time="2026-09-07 12:00:00.000",
            forced_start_time=None,
            lookback_seconds=None,
        ),
        seed_mode=AdapterReplaySeedMode.NONE,
        target_column_names=("event_time", "_replay_partition", "_replay_offset"),
    )
    statement: str = managed_clickhouse_client.render_replay_from_capture(
        AdapterCapturedReplayRequest(
            replay=replay,
            boundary_column_type=None,
            lower_bounds=(
                AdapterReplayLowerBound(value="2", partition_value="0"),
                AdapterReplayLowerBound(value="11", partition_value="1"),
            ),
        )
    )

    managed_clickhouse_client.execute_workflow_sql(statement)

    rows: tuple[tuple[object, ...], ...] = managed_clickhouse_client.query(
        f"""
        SELECT _replay_partition, _replay_offset
        FROM {clickhouse_database}.replayed_events
        ORDER BY _replay_partition, _replay_offset
        """
    ).rows
    assert rows == test_case.expected_positions


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
