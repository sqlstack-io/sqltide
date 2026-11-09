import pytest

from sqltide._native import validate_declared_external_sources
from sqltide.adapter.models import (
    AdapterIdentity,
    CatalogColumn,
    CatalogIdentity,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.cli.entry.exceptions import CliUserError
from tests.unit.src.sqltide.cli.plan.main._test_types import (
    CliExternalSourceValidationErrorTestCase,
)
from tests.unit.src.sqltide.cli.plan.main.helpers import (
    build_realized_external_source_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliExternalSourceValidationErrorTestCase(
            description="rejects replay alias collision with an existing physical column",
            existing_columns=(
                ("order_id", "String"),
                ("event_partition", "Int32"),
                ("event_offset", "Int64"),
                ("event_timestamp", "DateTime64(3)"),
                ("_replay_offset", "Int64"),
            ),
            expected_error_fragment="conflicts with the injected replay alias",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_external_source_alias_collision_when_validating_then_it_raises_clear_error(
    test_case: CliExternalSourceValidationErrorTestCase,
) -> None:
    catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="default"),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="orders_existing",
                engine="MergeTree",
                columns=tuple(
                    CatalogColumn(name=column_name, type=column_type)
                    for column_name, column_type in test_case.existing_columns
                ),
            ),
        ),
    )

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        validate_declared_external_sources(
            catalog=catalog,
            external_source_replay_configs=(
                build_realized_external_source_project().desired_state.external_source_replay_configs
            ),
            database="default",
        )


@pytest.mark.parametrize(
    "test_case",
    [
        CliExternalSourceValidationErrorTestCase(
            description="rejects non-integer offset mapping columns",
            existing_columns=(
                ("order_id", "String"),
                ("event_partition", "String"),
                ("event_offset", "Int64"),
                ("event_timestamp", "DateTime64(3)"),
            ),
            expected_error_fragment="partition column 'event_partition' with incompatible type",
        ),
        CliExternalSourceValidationErrorTestCase(
            description="rejects nested integer offset mapping columns",
            existing_columns=(
                ("order_id", "String"),
                ("event_partition", "Array(UInt64)"),
                ("event_offset", "Int64"),
                ("event_timestamp", "DateTime64(3)"),
            ),
            expected_error_fragment="partition column 'event_partition' with incompatible type",
        ),
        CliExternalSourceValidationErrorTestCase(
            description="rejects nested timestamp mapping columns",
            existing_columns=(
                ("order_id", "String"),
                ("event_partition", "Int32"),
                ("event_offset", "Int64"),
                ("event_timestamp", "Array(DateTime64(3))"),
            ),
            expected_error_fragment="timestamp column 'event_timestamp' with incompatible type",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_non_integer_offset_mapping_when_validating_then_it_raises_clear_error(
    test_case: CliExternalSourceValidationErrorTestCase,
) -> None:
    catalog: CatalogSnapshot = CatalogSnapshot(
        identity=CatalogIdentity(adapter=AdapterIdentity(name="clickhouse"), database="default"),
        warehouse_timezone="UTC",
        relations=(
            CatalogRelation(
                name="orders_existing",
                engine="MergeTree",
                columns=tuple(
                    CatalogColumn(name=column_name, type=column_type)
                    for column_name, column_type in test_case.existing_columns
                ),
            ),
        ),
    )

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        validate_declared_external_sources(
            catalog=catalog,
            external_source_replay_configs=(
                build_realized_external_source_project().desired_state.external_source_replay_configs
            ),
            database="default",
        )
