from pathlib import Path

import pytest

from sqltide._native import (
    build_facets_sql,
    build_record_sql,
    parse_messages_document,
)
from sqltide.adapter.models import AdapterQueryResult
from sqltide.dev_server.models import MessagesQueryRequest
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    MessageFacetsRouteTestCase,
    MessageListRouteTestCase,
    MessageRecordMissingTestCase,
    MessageRecordRouteTestCase,
    MessageRouteTestCase,
    MessageWideningRouteTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    MESSAGE_HEADER_SCHEMA_QUERY,
    MESSAGE_LIST_COLUMN_NAMES,
    MESSAGE_RECORD_COLUMN_NAMES,
    build_canned_messages_sql,
    build_message_test_client,
    build_test_client,
    write_dev_server_project,
)

_HEADER_SCHEMA_PRESENT: AdapterQueryResult = AdapterQueryResult(
    rows=((1,),), column_names=("present",)
)
_LIST_ROWS: tuple[tuple[object, ...], ...] = (
    (
        "2026-08-03 11:59:58.000",
        "2026-08-03 11:59:57.000",
        0,
        11,
        "OrderCreated",
        13,
        '{"message_type":"OrderCreated"}',
        700,
        ["trace-id"],
        ["ab12"],
    ),
    (
        "2026-08-03 11:59:57.000",
        None,
        0,
        10,
        "Cancel",
        6,
        '{"message_type":"Cancel"}',
        25,
        [],
        [],
    ),
)
_RECORD_ROW: tuple[object, ...] = (
    "2026-08-03 11:59:58.000",
    "2026-08-03 11:59:57.000",
    0,
    11,
    "source.orders",
    "OrderCreated",
    13,
    '{"message_type":"OrderCreated"}',
    32,
    ["trace-id"],
    ["ab12"],
)


@pytest.mark.parametrize(
    "test_case",
    [
        MessageListRouteTestCase(
            description="lists rows with headers, truncation flags, and a keyset cursor",
            limit=2,
            expected_keys=("OrderCreated", "Cancel"),
            expected_first_headers=(("trace-id", "ab12"),),
            expected_window_seconds=3600,
            expected_next_cursor={
                "landedAt": "2026-08-03 11:59:57.000",
                "partition": 0,
                "offset": 10,
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_landed_messages_when_listing_then_returns_rows_and_next_cursor(
    test_case: MessageListRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT,
            build_canned_messages_sql(
                limit=test_case.limit, window_seconds=3600
            ): AdapterQueryResult(rows=_LIST_ROWS, column_names=MESSAGE_LIST_COLUMN_NAMES),
        },
    )

    payload: dict = client.post(
        "/api/sources/orders/messages", json={"limit": test_case.limit}
    ).json()

    assert tuple(row["key"] for row in payload["rows"]) == test_case.expected_keys
    assert payload["rows"][0]["headers"] == [
        list(pair) for pair in test_case.expected_first_headers
    ]
    assert payload["rows"][0]["valueTruncated"] is True
    assert payload["rows"][1]["valueTruncated"] is False
    assert payload["rows"][1]["kafkaTimestamp"] is None
    assert payload["windowSeconds"] == test_case.expected_window_seconds
    assert payload["nextCursor"] == test_case.expected_next_cursor


@pytest.mark.parametrize(
    "test_case",
    [
        MessageWideningRouteTestCase(
            description="widens to the unbounded window when bounded windows are sparse",
            limit=2,
            expected_row_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sparse_landing_when_listing_newest_then_widens_to_unbounded_window(
    test_case: MessageWideningRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    empty: AdapterQueryResult = AdapterQueryResult(rows=(), column_names=MESSAGE_LIST_COLUMN_NAMES)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT,
            build_canned_messages_sql(limit=test_case.limit, window_seconds=3600): empty,
            build_canned_messages_sql(limit=test_case.limit, window_seconds=86400): empty,
            build_canned_messages_sql(
                limit=test_case.limit, window_seconds=None
            ): AdapterQueryResult(rows=_LIST_ROWS[:1], column_names=MESSAGE_LIST_COLUMN_NAMES),
        },
    )

    payload: dict = client.post(
        "/api/sources/orders/messages", json={"limit": test_case.limit}
    ).json()

    assert len(payload["rows"]) == test_case.expected_row_count
    assert payload["windowSeconds"] is None
    assert payload["nextCursor"] is None


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRecordRouteTestCase(
            description="returns the full stored record by replay coordinates",
            partition=0,
            offset=11,
            expected_value='{"message_type":"OrderCreated"}',
            expected_topic="source.orders",
            expected_headers=(("trace-id", "ab12"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_stored_record_when_fetching_then_returns_full_value(
    test_case: MessageRecordRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT,
            build_record_sql(
                database="analytics",
                relation_name="raw__orders",
                partition=test_case.partition,
                offset=test_case.offset,
            ): AdapterQueryResult(rows=(_RECORD_ROW,), column_names=MESSAGE_RECORD_COLUMN_NAMES),
        },
    )

    payload: dict = client.post(
        "/api/sources/orders/messages/record",
        json={"partition": test_case.partition, "offset": test_case.offset},
    ).json()

    assert payload["value"] == test_case.expected_value
    assert payload["topic"] == test_case.expected_topic
    assert payload["valueTruncated"] is False
    assert payload["headers"] == [list(pair) for pair in test_case.expected_headers]


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRecordMissingTestCase(
            description="responds not found for coordinates without a stored record",
            partition=0,
            offset=999,
            expected_fragment="no message at partition 0 offset 999",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_record_when_fetching_then_responds_not_found(
    test_case: MessageRecordMissingTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT,
            build_record_sql(
                database="analytics",
                relation_name="raw__orders",
                partition=test_case.partition,
                offset=test_case.offset,
            ): AdapterQueryResult(rows=(), column_names=MESSAGE_RECORD_COLUMN_NAMES),
        },
    )

    response: NativeResponse = client.post(
        "/api/sources/orders/messages/record",
        json={"partition": test_case.partition, "offset": test_case.offset},
    )

    assert response.status_code == 404
    assert test_case.expected_fragment in response.json()["detail"]


@pytest.mark.parametrize(
    "test_case",
    [
        MessageFacetsRouteTestCase(
            description="counts top values with explicit null and other buckets",
            expected_values=(("OrderCreated", 5), ("Cancel", 2)),
            expected_null_count=1,
            expected_other_count=1,
            expected_total_count=9,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_filtered_corpus_when_faceting_then_returns_counts_and_other_bucket(
    test_case: MessageFacetsRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    top_values_sql: str
    totals_sql: str
    top_values_sql, totals_sql = build_facets_sql(
        database="analytics",
        relation_name="raw__orders",
        document=parse_messages_document(MessagesQueryRequest()),
        facet_path=("message_type",),
    )
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT,
            top_values_sql: AdapterQueryResult(
                rows=test_case.expected_values,
                column_names=("facet_value", "n"),
            ),
            totals_sql: AdapterQueryResult(
                rows=((test_case.expected_total_count, test_case.expected_null_count),),
                column_names=("total", "null_count"),
            ),
        },
    )

    payload: dict = client.post(
        "/api/sources/orders/messages/facets", json={"facetPath": ["message_type"]}
    ).json()

    assert payload["values"] == [
        {"value": value, "count": count} for value, count in test_case.expected_values
    ]
    assert payload["nullCount"] == test_case.expected_null_count
    assert payload["otherCount"] == test_case.expected_other_count
    assert payload["totalCount"] == test_case.expected_total_count


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRouteTestCase(
            description="responds not found for an unknown source",
            path="/api/sources/nope/messages",
            body={},
            expected_status_code=404,
            expected_fragment="unknown source 'nope'",
        ),
        MessageRouteTestCase(
            description="responds bad request naming an invalid predicate field",
            path="/api/sources/orders/messages",
            body={"predicates": [{"field": "bogus", "op": "eq", "value": "x"}]},
            expected_status_code=400,
            expected_fragment="unsupported predicate field 'bogus'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_bad_message_request_when_posting_then_responds_with_named_error(
    test_case: MessageRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={MESSAGE_HEADER_SCHEMA_QUERY: _HEADER_SCHEMA_PRESENT},
    )

    response: NativeResponse = client.post(test_case.path, json=test_case.body)

    assert response.status_code == test_case.expected_status_code
    assert test_case.expected_fragment in response.json()["detail"]


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRouteTestCase(
            description="responds with the reset conflict for a pre-header raw table",
            path="/api/sources/orders/messages",
            body={},
            expected_status_code=409,
            expected_fragment="drop this pre-production raw table",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pre_header_raw_table_when_listing_then_responds_with_reset_conflict(
    test_case: MessageRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_message_test_client(
        project_dir=tmp_path,
        results_by_query={
            MESSAGE_HEADER_SCHEMA_QUERY: AdapterQueryResult(rows=((0,),), column_names=("present",))
        },
    )

    response: NativeResponse = client.post(test_case.path, json=test_case.body)

    assert response.status_code == test_case.expected_status_code
    assert test_case.expected_fragment in response.json()["detail"]


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRouteTestCase(
            description="responds service unavailable without a warehouse connection",
            path="/api/sources/orders/messages",
            body={},
            expected_status_code=503,
            expected_fragment="no warehouse connection",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_warehouse_connection_when_listing_then_responds_service_unavailable(
    test_case: MessageRouteTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.post(test_case.path, json=test_case.body)

    assert response.status_code == test_case.expected_status_code
    assert test_case.expected_fragment in response.json()["detail"]
