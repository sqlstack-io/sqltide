from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

import pytest

from sqltide._native import render_expected_warehouse_error, run_deployment_promotion
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.exceptions import (
    AdapterAuthenticationError,
    AdapterDatabaseNotFoundError,
    AdapterRelationNotFoundError,
    AdapterTimeoutError,
)
from sqltide.adapter.models import InspectedManagedTableState
from tests.unit.src.sqltide.cli._test_types import (
    CliCommandErrorTestCase,
    CliExpectedErrorRenderingTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliExpectedErrorRenderingTestCase(
            description="renders timeout failure message",
            error=AdapterTimeoutError("Request timed out"),
            expected_fragments=(
                "Publish could not complete",
                "Database: flights_demo",
                "ClickHouse operation timed out",
                "Retry the command",
            ),
        ),
        CliExpectedErrorRenderingTestCase(
            description="renders authentication failure message",
            error=AdapterAuthenticationError(
                "Code: 516. DB::Exception: Authentication failed. (AUTHENTICATION_FAILED)"
            ),
            expected_fragments=(
                "Publish could not start",
                "Database: flights_demo",
                "ClickHouse rejected the supplied credentials",
                "username and password",
            ),
        ),
        CliExpectedErrorRenderingTestCase(
            description="renders missing database message",
            error=AdapterDatabaseNotFoundError(
                "Code: 81. DB::Exception: Database flights_demo does not exist. (UNKNOWN_DATABASE)"
            ),
            expected_fragments=(
                "Publish could not start",
                "Database: flights_demo",
                "target ClickHouse database does not exist",
                "run sqt build first",
            ),
        ),
        CliExpectedErrorRenderingTestCase(
            description="renders missing metadata table message",
            error=AdapterRelationNotFoundError(
                "Code: 60. DB::Exception: Unknown table expression identifier "
                "_sqltide_virtual_deployments. (UNKNOWN_TABLE)"
            ),
            expected_fragments=(
                "Publish could not start",
                "Database: flights_demo",
                "SQLTide metadata tables do not exist",
                "run sqt build first",
            ),
        ),
        CliExpectedErrorRenderingTestCase(
            description="renders generic missing table message",
            error=AdapterRelationNotFoundError(
                "Code: 60. DB::Exception: Table flights_demo.tbl__orders does not exist. "
                "(UNKNOWN_TABLE)"
            ),
            expected_fragments=(
                "Publish could not start",
                "Database: flights_demo",
                "Table flights_demo.tbl__orders does not exist",
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_expected_warehouse_error_when_rendering_then_it_returns_operator_message(
    test_case: CliExpectedErrorRenderingTestCase,
) -> None:
    rendered: str | None = render_expected_warehouse_error(
        command_name="publish",
        database="flights_demo",
        error=test_case.error,
    )

    assert rendered is not None
    expected_fragment: str
    for expected_fragment in test_case.expected_fragments:
        assert expected_fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        CliCommandErrorTestCase(
            description="promotion handles missing database cleanly",
            error_message=(
                "Code: 81. DB::Exception: Database flights_demo does not exist. (UNKNOWN_DATABASE)"
            ),
            expected_exit_code=1,
            expected_error_fragments=(
                "Deployment Promote could not start",
                "Database: flights_demo",
                "run sqt build first",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_database_when_promoting_then_it_prints_friendly_error(
    test_case: CliCommandErrorTestCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingClient:
        def inspect_managed_table_state(self, database: str) -> InspectedManagedTableState:
            del database
            raise AdapterDatabaseNotFoundError(test_case.error_message)

        def query(self, _statement: str) -> object:
            raise AdapterDatabaseNotFoundError(test_case.error_message)

        def query_many(
            self,
            *,
            statement: str,
            decode: Callable[[Mapping[str, object]], object],
        ) -> tuple[object, ...]:
            self.query(statement)
            return ()

        def query_one(
            self,
            statement: str,
            *,
            decode: Callable[[Mapping[str, object]], object],
        ) -> object | None:
            self.query(statement)
            return None

        def close(self) -> None:
            return None

    exit_code: int = run_deployment_promotion(
        database="flights_demo",
        metadata_database=None,
        deployment_id=None,
        json_output=False,
        client=cast(AdapterConnection, FailingClient()),
    )
    captured_error: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    expected_fragment: str
    for expected_fragment in test_case.expected_error_fragments:
        assert expected_fragment in captured_error
