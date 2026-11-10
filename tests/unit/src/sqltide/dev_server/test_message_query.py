import pytest

from sqltide._native import (
    build_facets_sql,
    build_messages_sql,
    build_record_sql,
    parse_messages_document,
)
from sqltide.dev_server.exceptions import MessageQueryValidationError
from sqltide.dev_server.models import MessagesQueryRequest
from tests.unit.src.sqltide.dev_server._test_types import (
    MessageFacetsSqlTestCase,
    MessageQuerySqlTestCase,
    MessageQueryValidationTestCase,
    MessageRecordSqlTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import build_expected_messages_sql


@pytest.mark.parametrize(
    "test_case",
    [
        MessageQuerySqlTestCase(
            description="renders the bounded newest window with no predicates",
            request_json={},
            window_seconds=3600,
            expected_sql=build_expected_messages_sql(
                where_clause=" WHERE _replay_landed_at >= now64(3) - INTERVAL 3600 SECOND"
            ),
        ),
        MessageQuerySqlTestCase(
            description="renders a partition in-list predicate",
            request_json={"predicates": [{"field": "partition", "op": "in", "values": [1, 2]}]},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=" WHERE _replay_partition IN (1, 2)"
            ),
        ),
        MessageQuerySqlTestCase(
            description="escapes quotes and backslashes in key equality literals",
            request_json={"predicates": [{"field": "key", "op": "eq", "value": "O'Reilly\\x"}]},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=" WHERE kafka_key = 'O\\'Reilly\\\\x'"
            ),
        ),
        MessageQuerySqlTestCase(
            description="renders key contains and prefix through safe functions",
            request_json={
                "predicates": [
                    {"field": "key", "op": "contains", "value": "Order"},
                    {"field": "key", "op": "prefix", "value": "Order"},
                ]
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE position(kafka_key, 'Order') > 0 AND startsWith(kafka_key, 'Order')"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="renders a value contains predicate as position",
            request_json={"predicates": [{"field": "value", "op": "contains", "value": "cmeur"}]},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=" WHERE position(kafka_value, 'cmeur') > 0"
            ),
        ),
        MessageQuerySqlTestCase(
            description="renders every json predicate operator",
            request_json={
                "predicates": [
                    {"field": "json", "path": ["data", "source"], "op": "eq", "value": "partner"},
                    {"field": "json", "path": ["message_type"], "op": "ne", "value": "Cancel"},
                    {"field": "json", "path": ["data", "note"], "op": "contains", "value": "vip"},
                    {"field": "json", "path": ["data", 0, "id"], "op": "exists"},
                    {"field": "json", "path": ["data", "item_count"], "op": "gt", "value": 10},
                    {"field": "json", "path": ["data", "amount"], "op": "lt", "value": 2.5},
                ]
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE JSONExtractString(kafka_value, 'data', 'source') = 'partner'"
                    " AND JSONExtractString(kafka_value, 'message_type') != 'Cancel'"
                    " AND position(JSONExtractString(kafka_value, 'data', 'note'), 'vip') > 0"
                    " AND JSONHas(kafka_value, 'data', 0, 'id')"
                    " AND JSONExtractFloat(kafka_value, 'data', 'item_count') > 10.0"
                    " AND JSONExtractFloat(kafka_value, 'data', 'amount') < 2.5"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="renders header key and header value predicates over the arrays",
            request_json={
                "predicates": [
                    {"field": "header", "op": "eq", "value": "trace-id"},
                    {"field": "header", "op": "contains", "value": "ab12"},
                ]
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE has(kafka_header_keys, 'trace-id')"
                    " AND arrayExists(v -> position(v, 'ab12') > 0, kafka_header_values)"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="bounds a time range on the kafka timestamp column",
            request_json={
                "mode": {
                    "kind": "timeRange",
                    "fromTime": "2026-08-08 10:00:00",
                    "toTime": "2026-08-08 11:00:00",
                },
                "timeColumn": "kafka",
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE kafka_timestamp >= "
                    "parseDateTime64BestEffort('2026-08-08 10:00:00', 3)"
                    " AND kafka_timestamp <= "
                    "parseDateTime64BestEffort('2026-08-08 11:00:00', 3)"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="rides the primary key for an offset range in one partition",
            request_json={
                "mode": {"kind": "offsetRange", "partition": 1, "fromOffset": 5, "toOffset": 9}
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE _replay_partition = 1 AND _replay_offset >= 5 AND _replay_offset <= 9"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="appends the keyset cursor as a descending tuple bound",
            request_json={
                "cursor": {"landedAt": "2026-08-08 10:00:00.000", "partition": 1, "offset": 13}
            },
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                where_clause=(
                    " WHERE (_replay_landed_at, _replay_partition, _replay_offset) < "
                    "(parseDateTime64BestEffort('2026-08-08 10:00:00.000', 3), 1, 13)"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="adds preview field projections for chosen json paths",
            request_json={"previewPaths": [["message_type"], ["data", "source"]]},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(
                projections=(
                    ", JSONExtractString(kafka_value, 'message_type') AS preview_0"
                    ", JSONExtractString(kafka_value, 'data', 'source') AS preview_1"
                )
            ),
        ),
        MessageQuerySqlTestCase(
            description="clamps an oversized limit to the maximum",
            request_json={"limit": 9999},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(limit=500),
        ),
        MessageQuerySqlTestCase(
            description="clamps a zero limit to the minimum",
            request_json={"limit": 0},
            window_seconds=None,
            expected_sql=build_expected_messages_sql(limit=1),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_typed_document_when_building_messages_sql_then_renders_exact_statement(
    test_case: MessageQuerySqlTestCase,
) -> None:
    sql: str = build_messages_sql(
        database="analytics",
        relation_name="raw__orders",
        document=parse_messages_document(MessagesQueryRequest(**test_case.request_json)),
        window_seconds=test_case.window_seconds,
    )

    assert sql == test_case.expected_sql


@pytest.mark.parametrize(
    "test_case",
    [
        MessageQueryValidationTestCase(
            description="rejects an unknown predicate field by name",
            request_json={"predicates": [{"field": "bogus", "op": "eq", "value": "x"}]},
            expected_error_fragment="unsupported predicate field 'bogus'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unknown mode kind",
            request_json={"mode": {"kind": "stream"}},
            expected_error_fragment="unsupported mode kind 'stream'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unknown time column",
            request_json={"timeColumn": "produced"},
            expected_error_fragment="unsupported time column 'produced'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unsupported key operator",
            request_json={"predicates": [{"field": "key", "op": "regex", "value": "x"}]},
            expected_error_fragment="unsupported op 'regex' for field 'key'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unsupported partition operator",
            request_json={"predicates": [{"field": "partition", "op": "eq", "values": [1]}]},
            expected_error_fragment="unsupported op 'eq' for field 'partition'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an empty partition value list",
            request_json={"predicates": [{"field": "partition", "op": "in", "values": []}]},
            expected_error_fragment="field 'partition' requires at least one value",
        ),
        MessageQueryValidationTestCase(
            description="rejects negative partition values",
            request_json={"predicates": [{"field": "partition", "op": "in", "values": [-1]}]},
            expected_error_fragment="field 'partition' requires non-negative integers",
        ),
        MessageQueryValidationTestCase(
            description="rejects a numeric value for key equality",
            request_json={"predicates": [{"field": "key", "op": "eq", "value": 7}]},
            expected_error_fragment="field 'key' requires a string value",
        ),
        MessageQueryValidationTestCase(
            description="rejects a string value for numeric json comparison",
            request_json={
                "predicates": [{"field": "json", "path": ["n"], "op": "gt", "value": "10"}]
            },
            expected_error_fragment="field 'json' requires a numeric value for gt/lt",
        ),
        MessageQueryValidationTestCase(
            description="rejects a json predicate without a path",
            request_json={"predicates": [{"field": "json", "op": "eq", "value": "x"}]},
            expected_error_fragment="field 'json' requires a JSON path",
        ),
        MessageQueryValidationTestCase(
            description="rejects json paths deeper than eight segments",
            request_json={
                "predicates": [
                    {
                        "field": "json",
                        "path": ["a", "b", "c", "d", "e", "f", "g", "h", "i"],
                        "op": "exists",
                    }
                ]
            },
            expected_error_fragment="depth at most 8",
        ),
        MessageQueryValidationTestCase(
            description="rejects quote injection inside json path segments",
            request_json={
                "predicates": [{"field": "json", "path": ["a') OR ('1'='1"], "op": "exists"}]
            },
            expected_error_fragment="path segments cannot contain quotes",
        ),
        MessageQueryValidationTestCase(
            description="rejects newline control characters inside json path segments",
            request_json={"predicates": [{"field": "json", "path": ["a\nb"], "op": "exists"}]},
            expected_error_fragment="path segments cannot contain quotes",
        ),
        MessageQueryValidationTestCase(
            description="rejects json path segments longer than sixty-four characters",
            request_json={"predicates": [{"field": "json", "path": ["s" * 65], "op": "exists"}]},
            expected_error_fragment="1 to 64 characters",
        ),
        MessageQueryValidationTestCase(
            description="rejects more than four preview paths",
            request_json={"previewPaths": [["a"], ["b"], ["c"], ["d"], ["e"]]},
            expected_error_fragment="previewPaths allows at most 4 paths",
        ),
        MessageQueryValidationTestCase(
            description="rejects a time range without bounds",
            request_json={"mode": {"kind": "timeRange"}},
            expected_error_fragment="timeRange mode requires fromTime or toTime",
        ),
        MessageQueryValidationTestCase(
            description="rejects a malformed time bound",
            request_json={"mode": {"kind": "timeRange", "fromTime": "not-a-time"}},
            expected_error_fragment="field 'fromTime' requires an ISO 8601 timestamp",
        ),
        MessageQueryValidationTestCase(
            description="rejects an offset range without a partition",
            request_json={"mode": {"kind": "offsetRange", "fromOffset": 1}},
            expected_error_fragment="offsetRange mode requires exactly one partition",
        ),
        MessageQueryValidationTestCase(
            description="rejects an inverted offset range",
            request_json={
                "mode": {"kind": "offsetRange", "partition": 0, "fromOffset": 9, "toOffset": 5}
            },
            expected_error_fragment="fromOffset must not exceed toOffset",
        ),
        MessageQueryValidationTestCase(
            description="rejects a cursor with a malformed landed timestamp",
            request_json={"cursor": {"landedAt": "yesterday", "partition": 0, "offset": 1}},
            expected_error_fragment="field 'cursor.landedAt' requires an ISO 8601 timestamp",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unsupported header operator",
            request_json={"predicates": [{"field": "header", "op": "prefix", "value": "t"}]},
            expected_error_fragment="unsupported op 'prefix' for field 'header'",
        ),
        MessageQueryValidationTestCase(
            description="rejects an unsupported value operator",
            request_json={"predicates": [{"field": "value", "op": "eq", "value": "x"}]},
            expected_error_fragment="unsupported op 'eq' for field 'value'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_document_when_parsing_then_names_the_offending_field(
    test_case: MessageQueryValidationTestCase,
) -> None:
    with pytest.raises(MessageQueryValidationError) as error_info:
        parse_messages_document(MessagesQueryRequest(**test_case.request_json))

    assert test_case.expected_error_fragment in str(error_info.value)


@pytest.mark.parametrize(
    "test_case",
    [
        MessageRecordSqlTestCase(
            description="renders the primary-key record lookup",
            partition=1,
            offset=13673,
            expected_sql=(
                "SELECT toString(_replay_landed_at) AS landed_at, "
                "toString(kafka_timestamp) AS kafka_timestamp, "
                "_replay_partition AS partition, _replay_offset AS offset, "
                "kafka_topic AS topic, kafka_key AS key, length(kafka_key) AS key_bytes, "
                "substring(kafka_value, 1, 16777216) AS value, "
                "length(kafka_value) AS value_bytes, "
                "kafka_header_keys AS header_keys, kafka_header_values AS header_values "
                "FROM `analytics`.`raw__orders` "
                "WHERE _replay_partition = 1 AND _replay_offset = 13673 LIMIT 1"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_replay_coordinates_when_building_record_sql_then_renders_exact_statement(
    test_case: MessageRecordSqlTestCase,
) -> None:
    sql: str = build_record_sql(
        database="analytics",
        relation_name="raw__orders",
        partition=test_case.partition,
        offset=test_case.offset,
    )

    assert sql == test_case.expected_sql


@pytest.mark.parametrize(
    "test_case",
    [
        MessageFacetsSqlTestCase(
            description="facets share predicates, bound newest to a day, and ignore the cursor",
            request_json={
                "predicates": [{"field": "key", "op": "contains", "value": "Order"}],
                "cursor": {"landedAt": "2026-08-08 10:00:00.000", "partition": 0, "offset": 5},
            },
            facet_path=("message_type",),
            expected_top_values_sql=(
                "SELECT facet_value, count() AS n FROM ("
                "SELECT JSONExtractString(kafka_value, 'message_type') AS facet_value "
                "FROM `analytics`.`raw__orders` WHERE position(kafka_key, 'Order') > 0 "
                "AND _replay_landed_at >= now64(3) - INTERVAL 86400 SECOND) "
                "WHERE facet_value != '' GROUP BY facet_value "
                "ORDER BY n DESC, facet_value ASC LIMIT 20"
            ),
            expected_totals_sql=(
                "SELECT count() AS total, countIf(facet_value = '') AS null_count FROM ("
                "SELECT JSONExtractString(kafka_value, 'message_type') AS facet_value "
                "FROM `analytics`.`raw__orders` WHERE position(kafka_key, 'Order') > 0 "
                "AND _replay_landed_at >= now64(3) - INTERVAL 86400 SECOND)"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_facet_request_when_building_facets_sql_then_renders_exact_statements(
    test_case: MessageFacetsSqlTestCase,
) -> None:
    top_values_sql: str
    totals_sql: str
    top_values_sql, totals_sql = build_facets_sql(
        database="analytics",
        relation_name="raw__orders",
        document=parse_messages_document(MessagesQueryRequest(**test_case.request_json)),
        facet_path=test_case.facet_path,
    )

    assert top_values_sql == test_case.expected_top_values_sql
    assert totals_sql == test_case.expected_totals_sql
