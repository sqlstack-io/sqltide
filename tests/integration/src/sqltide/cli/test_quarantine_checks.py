import pytest
from clickhouse_connect.driver.client import Client

import sqltide._native as _native
from tests.integration.src.sqltide.cli._test_types import QuarantineCheckAgreementTestCase
from tests.integration.src.sqltide.cli.helpers import real_cast_row_count


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        QuarantineCheckAgreementTestCase(
            description="UInt8 from Int64 300",
            target_type="UInt8",
            source_type="Int64",
            value_sql="300",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="UInt8 from Int64 -1",
            target_type="UInt8",
            source_type="Int64",
            value_sql="-1",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="UInt8 from String '300'",
            target_type="UInt8",
            source_type="String",
            value_sql="'300'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="UInt8 from String '-1'",
            target_type="UInt8",
            source_type="String",
            value_sql="'-1'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int32 from Float64 1.5",
            target_type="Int32",
            source_type="Float64",
            value_sql="1.5",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int32 from String '7'",
            target_type="Int32",
            source_type="String",
            value_sql="'7'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int32 from String 'abc'",
            target_type="Int32",
            source_type="String",
            value_sql="'abc'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int32 from String '99999999999'",
            target_type="Int32",
            source_type="String",
            value_sql="'99999999999'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int32 from Nullable(String) NULL",
            target_type="Int32",
            source_type="Nullable(String)",
            value_sql="NULL",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Int64 from String '99999999999999999999999'",
            target_type="Int64",
            source_type="String",
            value_sql="'99999999999999999999999'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Float32 from Float64 1e300",
            target_type="Float32",
            source_type="Float64",
            value_sql="1e300",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Float64 from String 'x'",
            target_type="Float64",
            source_type="String",
            value_sql="'x'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date from String '2026-01-01'",
            target_type="Date",
            source_type="String",
            value_sql="'2026-01-01'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date from String 'bad'",
            target_type="Date",
            source_type="String",
            value_sql="'bad'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date from String '9999-99-99'",
            target_type="Date",
            source_type="String",
            value_sql="'9999-99-99'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date32 from String 'bad'",
            target_type="Date32",
            source_type="String",
            value_sql="'bad'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="DateTime from String 'bad'",
            target_type="DateTime",
            source_type="String",
            value_sql="'bad'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="DateTime from Int64 99999999999",
            target_type="DateTime",
            source_type="Int64",
            value_sql="99999999999",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="DateTime64(3) from String 'bad'",
            target_type="DateTime64(3)",
            source_type="String",
            value_sql="'bad'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="String from Nullable(String) NULL",
            target_type="String",
            source_type="Nullable(String)",
            value_sql="NULL",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="String from Int32 5",
            target_type="String",
            source_type="Int32",
            value_sql="5",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="UUID from String 'x'",
            target_type="UUID",
            source_type="String",
            value_sql="'x'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="IPv4 from String 'x'",
            target_type="IPv4",
            source_type="String",
            value_sql="'x'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="IPv6 from String 'x'",
            target_type="IPv6",
            source_type="String",
            value_sql="'x'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Decimal(10, 2) from String '1.25'",
            target_type="Decimal(10, 2)",
            source_type="String",
            value_sql="'1.25'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Decimal(10, 2) from String 'x'",
            target_type="Decimal(10, 2)",
            source_type="String",
            value_sql="'x'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Decimal(10, 2) from Float64 1e20",
            target_type="Decimal(10, 2)",
            source_type="Float64",
            value_sql="1e20",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="FixedString(2) from String 'ab'",
            target_type="FixedString(2)",
            source_type="String",
            value_sql="'ab'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="FixedString(2) from String 'abc'",
            target_type="FixedString(2)",
            source_type="String",
            value_sql="'abc'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="LowCardinality(String) from Nullable(String) NULL",
            target_type="LowCardinality(String)",
            source_type="Nullable(String)",
            value_sql="NULL",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Bool from String 'true'",
            target_type="Bool",
            source_type="String",
            value_sql="'true'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Bool from String 'nope'",
            target_type="Bool",
            source_type="String",
            value_sql="'nope'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Bool from String '1'",
            target_type="Bool",
            source_type="String",
            value_sql="'1'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Bool from Int32 2",
            target_type="Bool",
            source_type="Int32",
            value_sql="2",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Bool from Nullable(String) NULL",
            target_type="Bool",
            source_type="Nullable(String)",
            value_sql="NULL",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date from String '2300-01-01'",
            target_type="Date",
            source_type="String",
            value_sql="'2300-01-01'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="Date from String '2026-02-31'",
            target_type="Date",
            source_type="String",
            value_sql="'2026-02-31'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="DateTime from String '2300-01-01'",
            target_type="DateTime",
            source_type="String",
            value_sql="'2300-01-01'",
            expected_check_agrees_with_cast=True,
        ),
        QuarantineCheckAgreementTestCase(
            description="DateTime64(3) from String '2300-01-01'",
            target_type="DateTime64(3)",
            source_type="String",
            value_sql="'2300-01-01'",
            expected_check_agrees_with_cast=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_a_value_when_checking_then_only_rows_cast_would_raise_on_are_invalid(
    test_case: QuarantineCheckAgreementTestCase,
    clickhouse_client: Client,
    clickhouse_database: str,
) -> None:
    table: str = f"{clickhouse_database}.agreement_input"
    clickhouse_client.command(f"DROP TABLE IF EXISTS {table} SYNC")
    clickhouse_client.command(
        f"CREATE TABLE {table} (x {test_case.source_type}, k UInt8) ENGINE = Memory"
    )
    clickhouse_client.command(f"INSERT INTO {table} VALUES ({test_case.value_sql}, 1)")
    good_query, _ = _native.sql_quarantine_split(
        f"SELECT CAST(x AS {test_case.target_type}) AS v FROM {table}", "orders", "clickhouse"
    )

    good_rows: int = len(clickhouse_client.query(good_query).result_rows)
    cast_rows: int = real_cast_row_count(
        client=clickhouse_client, table=table, target_type=test_case.target_type
    )

    assert (good_rows == cast_rows) is test_case.expected_check_agrees_with_cast


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
