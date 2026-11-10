import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from sqltide._native import translate_clickhouse_driver_error
from sqltide.adapter.exceptions import (
    AdapterAuthenticationError,
    AdapterDatabaseNotFoundError,
    AdapterRelationNotFoundError,
    AdapterTimeoutError,
    AdapterWarehouseError,
)
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    DriverErrorTranslationTestCase,
)

_AUTHENTICATION_FAILED_MESSAGE: str = (
    "Code: 516. DB::Exception: Authentication failed. (AUTHENTICATION_FAILED)"
)
_UNKNOWN_DATABASE_MESSAGE: str = (
    "Code: 81. DB::Exception: Database analytics does not exist. (UNKNOWN_DATABASE)"
)
_UNKNOWN_TABLE_MESSAGE: str = (
    "Code: 60. DB::Exception: Table analytics.tbl__orders does not exist. (UNKNOWN_TABLE)"
)
_TIMEOUT_MESSAGE: str = (
    "Error ('Connection aborted.', TimeoutError('timed out')) executing HTTP request attempt 1"
)


@pytest.mark.parametrize(
    "test_case",
    [
        DriverErrorTranslationTestCase(
            description="translates a missing relation error",
            driver_error=DatabaseError(_UNKNOWN_TABLE_MESSAGE),
            expected_error_type=AdapterRelationNotFoundError,
            expected_message=_UNKNOWN_TABLE_MESSAGE,
        ),
        DriverErrorTranslationTestCase(
            description="translates a missing database error",
            driver_error=DatabaseError(_UNKNOWN_DATABASE_MESSAGE),
            expected_error_type=AdapterDatabaseNotFoundError,
            expected_message=_UNKNOWN_DATABASE_MESSAGE,
        ),
        DriverErrorTranslationTestCase(
            description="translates an authentication failure error",
            driver_error=OperationalError(_AUTHENTICATION_FAILED_MESSAGE),
            expected_error_type=AdapterAuthenticationError,
            expected_message=_AUTHENTICATION_FAILED_MESSAGE,
        ),
        DriverErrorTranslationTestCase(
            description="translates a request timeout into a neutral timeout error",
            driver_error=OperationalError(_TIMEOUT_MESSAGE),
            expected_error_type=AdapterTimeoutError,
            expected_message=_TIMEOUT_MESSAGE,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_driver_error_when_translating_then_it_returns_the_neutral_equivalent(
    test_case: DriverErrorTranslationTestCase,
) -> None:
    translated: AdapterWarehouseError = translate_clickhouse_driver_error(test_case.driver_error)

    assert type(translated) is test_case.expected_error_type
    assert str(translated) == test_case.expected_message
