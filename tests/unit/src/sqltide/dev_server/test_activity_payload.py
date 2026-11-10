import pytest

from sqltide._native import (
    build_activity_capabilities_query,
    build_part_log_activity_query,
    build_parts_activity_query,
    build_query_views_activity_query,
    read_model_activity,
)
from sqltide.adapter.models import (
    AdapterIdentity,
    AdapterQueryResult,
    CatalogIdentity,
    CatalogSnapshot,
)
from sqltide.dev_server.constants import ACTIVITY_WINDOW_SECONDS
from tests.unit.src.sqltide.dev_server._test_types import ActivityPayloadTestCase
from tests.unit.src.sqltide.dev_server.helpers import FakeAdapterConnection

_DATABASE: str = "analytics"
_RELATION: str = "tbl__orders"
_CAPTURED_AT: str = "2026-08-09 12:00:00.000"


@pytest.mark.parametrize(
    "test_case",
    [
        ActivityPayloadTestCase(
            description="materialized view writes are primary moving evidence",
            capabilities=("part_log", "query_views_log"),
            view_rows=(
                (
                    "2026-08-09 11:59:30.000000",
                    "analytics.`tbl__orders`",
                    12,
                    "QueryFinish",
                ),
            ),
            part_log_rows=(("2026-08-09 11:59:40.000000", "tbl__orders", 99),),
            parts_rows=(("tbl__orders", "2026-08-09 11:59:50"),),
            expected_state="moving",
            expected_source="query_views_log",
            expected_approximate=False,
            expected_rows_written=12,
            expected_last_triggered_at="2026-08-09 11:59:30.000000",
        ),
        ActivityPayloadTestCase(
            description="zero-output materialized view execution is idle",
            capabilities=("query_views_log",),
            view_rows=(("2026-08-09 11:59:30.000000", "analytics.tbl__orders", 0, "QueryFinish"),),
            part_log_rows=(),
            parts_rows=(),
            expected_state="idle",
            expected_source="query_views_log",
            expected_approximate=False,
            expected_rows_written=0,
            expected_last_triggered_at="2026-08-09 11:59:30.000000",
        ),
        ActivityPayloadTestCase(
            description="latest materialized view exception is stalled",
            capabilities=("query_views_log",),
            view_rows=(
                (
                    "2026-08-09 11:59:30.000000",
                    "analytics.tbl__orders",
                    0,
                    "ExceptionWhileProcessing",
                ),
            ),
            part_log_rows=(),
            parts_rows=(),
            expected_state="stalled",
            expected_source="query_views_log",
            expected_approximate=False,
            expected_rows_written=0,
            expected_last_triggered_at="2026-08-09 11:59:30.000000",
        ),
        ActivityPayloadTestCase(
            description="new parts are fallback moving evidence",
            capabilities=("part_log",),
            view_rows=(),
            part_log_rows=(("2026-08-09 11:59:40.000000", "tbl__orders", 7),),
            parts_rows=(),
            expected_state="moving",
            expected_source="part_log",
            expected_approximate=False,
            expected_rows_written=7,
            expected_last_triggered_at="2026-08-09 11:59:40.000000",
        ),
        ActivityPayloadTestCase(
            description="recent active part is explicitly approximate moving evidence",
            capabilities=(),
            view_rows=(),
            part_log_rows=(),
            parts_rows=(("tbl__orders", "2026-08-09 11:59:50"),),
            expected_state="moving",
            expected_source="system_parts",
            expected_approximate=True,
            expected_rows_written=0,
            expected_last_triggered_at=None,
        ),
        ActivityPayloadTestCase(
            description="active deployment binding maps physical part evidence to logical model",
            capabilities=(),
            view_rows=(),
            part_log_rows=(),
            parts_rows=(("tbl__orders__20260810T103523Z_bd67d9", "2026-08-09 11:59:50"),),
            expected_state="moving",
            expected_source="system_parts",
            expected_approximate=True,
            expected_rows_written=0,
            expected_last_triggered_at=None,
            physical_relation_name="tbl__orders__20260810T103523Z_bd67d9",
        ),
        ActivityPayloadTestCase(
            description="available log without recent evidence is idle",
            capabilities=("query_views_log",),
            view_rows=(),
            part_log_rows=(),
            parts_rows=(),
            expected_state="idle",
            expected_source="query_views_log",
            expected_approximate=False,
            expected_rows_written=0,
            expected_last_triggered_at=None,
        ),
        ActivityPayloadTestCase(
            description="missing logs and parts remain unknown",
            capabilities=(),
            view_rows=(),
            part_log_rows=(),
            parts_rows=(),
            expected_state="unknown",
            expected_source="unavailable",
            expected_approximate=False,
            expected_rows_written=0,
            expected_last_triggered_at=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_clickhouse_evidence_when_reading_activity_then_uses_strict_priority(
    test_case: ActivityPayloadTestCase,
) -> None:
    connection: FakeAdapterConnection = FakeAdapterConnection(
        catalog=CatalogSnapshot(
            identity=CatalogIdentity(
                adapter=AdapterIdentity(name="clickhouse"), database=_DATABASE
            ),
            warehouse_timezone="UTC",
            relations=(),
        ),
        warehouse_timestamp=_CAPTURED_AT,
        results_by_query={
            build_activity_capabilities_query(): AdapterQueryResult(
                rows=tuple((name,) for name in test_case.capabilities),
                column_names=("name",),
            ),
            build_query_views_activity_query(
                database=_DATABASE, window_seconds=ACTIVITY_WINDOW_SECONDS
            ): AdapterQueryResult(
                rows=test_case.view_rows,
                column_names=("observed_at", "view_target", "written_rows", "status"),
            ),
            build_part_log_activity_query(
                database=_DATABASE, window_seconds=ACTIVITY_WINDOW_SECONDS
            ): AdapterQueryResult(
                rows=test_case.part_log_rows,
                column_names=("observed_at", "table", "rows"),
            ),
            build_parts_activity_query(database=_DATABASE): AdapterQueryResult(
                rows=test_case.parts_rows,
                column_names=("table", "last_modified_at"),
            ),
        },
    )

    activity: dict[str, object] = read_model_activity(
        connection=connection,
        database=_DATABASE,
        relation_names=(_RELATION,),
        captured_at=_CAPTURED_AT,
        active_bindings=((_RELATION, test_case.physical_relation_name),),
    )[_RELATION]

    assert activity["state"] == test_case.expected_state
    assert activity["source"] == test_case.expected_source
    assert activity["approximate"] is test_case.expected_approximate
    assert activity["rowsWritten"] == test_case.expected_rows_written
    assert activity["lastTriggeredAt"] == test_case.expected_last_triggered_at
