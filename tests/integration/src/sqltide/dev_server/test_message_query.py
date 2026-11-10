from typing import cast

import pytest
from clickhouse_connect.driver.client import Client

from sqltide._native import (
    ensure_header_columns,
    read_source_message_facets,
    read_source_message_record,
    read_source_messages,
)
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.dev_server.exceptions import MessageSchemaError
from sqltide.dev_server.models import (
    MessageFacetsRequest,
    MessageQueryPredicate,
    MessagesQueryRequest,
)
from tests.integration.src.sqltide.adapters.clickhouse.helpers import connect_clickhouse
from tests.integration.src.sqltide.conftest import ClickHouseConnectionSettings
from tests.integration.src.sqltide.dev_server._test_types import (
    MessageCorpusQueryTestCase,
    MessageFacetsCorpusTestCase,
    MessagePaginationTestCase,
    MessageSchemaResetTestCase,
    MessageTruncationTestCase,
)
from tests.integration.src.sqltide.dev_server.helpers import (
    MESSAGE_CORPUS_LONG_VALUE,
    MESSAGE_CORPUS_RELATION_NAME,
    create_message_corpus,
    create_pre_header_raw_table,
    fetch_first_message_page,
    fetch_message_page_after,
    page_coordinates,
    page_cursor,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MessageCorpusQueryTestCase(
            description="newest mode returns fresh rows inside the first hour window",
            request_json={"limit": 3},
            expected_coordinates=((0, 2), (1, 1), (1, 2)),
            expected_window_seconds=3600,
            expected_has_next_cursor=True,
        ),
        MessageCorpusQueryTestCase(
            description="newest mode widens to a day when the hour is too sparse",
            request_json={"limit": 4},
            expected_coordinates=((0, 2), (1, 1), (1, 2), (0, 1)),
            expected_window_seconds=86400,
            expected_has_next_cursor=True,
        ),
        MessageCorpusQueryTestCase(
            description="newest mode ends unbounded when even a day cannot satisfy the limit",
            request_json={"limit": 10},
            expected_coordinates=((0, 2), (1, 1), (1, 2), (0, 1), (2, 5)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="json equality pushes down to the payload source",
            request_json={
                "limit": 10,
                "predicates": [
                    {"field": "json", "path": ["data", "source"], "op": "eq", "value": "partner"}
                ],
            },
            expected_coordinates=((1, 1), (0, 1)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="json numeric comparison filters on extracted floats",
            request_json={
                "limit": 10,
                "predicates": [
                    {"field": "json", "path": ["data", "item_count"], "op": "gt", "value": 10}
                ],
            },
            expected_coordinates=((0, 1),),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="header key equality matches rows carrying the header",
            request_json={
                "limit": 10,
                "predicates": [{"field": "header", "op": "eq", "value": "trace-id"}],
            },
            expected_coordinates=((1, 1), (0, 1)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="header value contains searches inside header values",
            request_json={
                "limit": 10,
                "predicates": [{"field": "header", "op": "contains", "value": "t3"}],
            },
            expected_coordinates=((1, 1),),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="value contains searches the raw payload bytes",
            request_json={
                "limit": 10,
                "predicates": [{"field": "value", "op": "contains", "value": '"Order"'}],
            },
            expected_coordinates=((1, 2),),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="partition in-list narrows to chosen partitions",
            request_json={
                "limit": 10,
                "predicates": [{"field": "partition", "op": "in", "values": [1]}],
            },
            expected_coordinates=((1, 1), (1, 2)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="offset range rides the primary key in one partition",
            request_json={
                "limit": 10,
                "mode": {
                    "kind": "offsetRange",
                    "partition": 0,
                    "fromOffset": 1,
                    "toOffset": 2,
                },
            },
            expected_coordinates=((0, 2), (0, 1)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
        MessageCorpusQueryTestCase(
            description="key prefix matches only keys starting with the literal",
            request_json={
                "limit": 10,
                "predicates": [{"field": "key", "op": "prefix", "value": "OrderCreated"}],
            },
            expected_coordinates=((0, 2), (0, 1)),
            expected_window_seconds=None,
            expected_has_next_cursor=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_landed_corpus_when_querying_then_filters_orders_and_windows_honestly(
    test_case: MessageCorpusQueryTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    create_message_corpus(clickhouse_client=clickhouse_client, database=clickhouse_database)
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        payload: dict[str, object] = read_source_messages(
            connection=connection,
            database=clickhouse_database,
            relation_name=MESSAGE_CORPUS_RELATION_NAME,
            request=MessagesQueryRequest(**test_case.request_json),
        )
    finally:
        connection.close()

    rows: list[dict[str, object]] = cast(list[dict[str, object]], payload["rows"])
    assert (
        tuple((row["partition"], row["offset"]) for row in rows) == test_case.expected_coordinates
    )
    assert payload["windowSeconds"] == test_case.expected_window_seconds
    assert (payload["nextCursor"] is not None) is test_case.expected_has_next_cursor


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MessagePaginationTestCase(
            description="keyset cursor walks every corpus row exactly once",
            page_limit=2,
            expected_walk=((0, 2), (1, 1), (1, 2), (0, 1), (2, 5)),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_landed_corpus_when_paging_then_keyset_cursor_walks_without_overlap(
    test_case: MessagePaginationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    create_message_corpus(clickhouse_client=clickhouse_client, database=clickhouse_database)
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        first_page: dict[str, object] = fetch_first_message_page(
            connection=connection, database=clickhouse_database, limit=test_case.page_limit
        )
        second_page: dict[str, object] = fetch_message_page_after(
            connection=connection,
            database=clickhouse_database,
            limit=test_case.page_limit,
            cursor=page_cursor(first_page),
        )
        third_page: dict[str, object] = fetch_message_page_after(
            connection=connection,
            database=clickhouse_database,
            limit=test_case.page_limit,
            cursor=page_cursor(second_page),
        )
    finally:
        connection.close()

    walked: tuple[tuple[object, object], ...] = (
        page_coordinates(first_page) + page_coordinates(second_page) + page_coordinates(third_page)
    )
    assert walked == test_case.expected_walk
    assert third_page["nextCursor"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MessageTruncationTestCase(
            description="list previews truncate while records return complete bytes",
            expected_preview_chars=512,
            expected_duplicate_headers=(("trace-id", "t2"), ("trace-id", "t3")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_long_payload_when_listing_then_preview_truncates_and_record_returns_all(
    test_case: MessageTruncationTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    create_message_corpus(clickhouse_client=clickhouse_client, database=clickhouse_database)
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        listing: dict[str, object] = read_source_messages(
            connection=connection,
            database=clickhouse_database,
            relation_name=MESSAGE_CORPUS_RELATION_NAME,
            request=MessagesQueryRequest(
                limit=10,
                predicates=[MessageQueryPredicate(field="key", op="eq", value="Order")],
            ),
        )
        record: dict[str, object] | None = read_source_message_record(
            connection=connection,
            database=clickhouse_database,
            relation_name=MESSAGE_CORPUS_RELATION_NAME,
            partition=1,
            offset=2,
        )
        duplicate_header_record: dict[str, object] | None = read_source_message_record(
            connection=connection,
            database=clickhouse_database,
            relation_name=MESSAGE_CORPUS_RELATION_NAME,
            partition=1,
            offset=1,
        )
    finally:
        connection.close()

    listed_row: dict[str, object] = cast("list[dict[str, object]]", listing["rows"])[0]
    assert len(str(listed_row["valuePreview"])) == test_case.expected_preview_chars
    assert listed_row["valueTruncated"] is True
    assert listed_row["valueBytes"] == len(MESSAGE_CORPUS_LONG_VALUE)
    assert record is not None
    assert record["value"] == MESSAGE_CORPUS_LONG_VALUE
    assert record["valueTruncated"] is False
    assert duplicate_header_record is not None
    assert duplicate_header_record["headers"] == [
        list(pair) for pair in test_case.expected_duplicate_headers
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MessageFacetsCorpusTestCase(
            description="facet counts include explicit null and window buckets",
            expected_values=(("OrderCreated", 2), ("Cancel", 1), ("Order", 1)),
            expected_null_count=1,
            expected_total_count=5,
            expected_window_seconds=86400,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_landed_corpus_when_faceting_then_counts_values_and_null_bucket(
    test_case: MessageFacetsCorpusTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    create_message_corpus(clickhouse_client=clickhouse_client, database=clickhouse_database)
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        payload: dict[str, object] = read_source_message_facets(
            connection=connection,
            database=clickhouse_database,
            relation_name=MESSAGE_CORPUS_RELATION_NAME,
            request=MessageFacetsRequest(facetPath=["message_type"]),
        )
    finally:
        connection.close()

    assert payload["values"] == [
        {"value": value, "count": count} for value, count in test_case.expected_values
    ]
    assert payload["nullCount"] == test_case.expected_null_count
    assert payload["otherCount"] == 0
    assert payload["totalCount"] == test_case.expected_total_count
    assert payload["windowSeconds"] == test_case.expected_window_seconds


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        MessageSchemaResetTestCase(
            description="pre-header raw tables fail with the actionable reset error",
            expected_error_fragment="drop this pre-production raw table",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pre_header_raw_table_when_checking_schema_then_raises_actionable_reset(
    test_case: MessageSchemaResetTestCase,
    clickhouse_connection_settings: ClickHouseConnectionSettings,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    create_pre_header_raw_table(clickhouse_client=clickhouse_client, database=clickhouse_database)
    connection: AdapterConnection = connect_clickhouse(
        connection_settings=clickhouse_connection_settings, database=clickhouse_database
    )

    try:
        with pytest.raises(MessageSchemaError, match=test_case.expected_error_fragment):
            ensure_header_columns(
                connection=connection,
                database=clickhouse_database,
                relation_name="raw__legacy",
            )
    finally:
        connection.close()
