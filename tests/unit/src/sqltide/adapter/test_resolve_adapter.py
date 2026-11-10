import pytest

from sqltide._native import build_adapter_registry, builtin_adapters, resolve_adapter
from sqltide.adapter.classes.adapter import Adapter
from sqltide.adapter.exceptions import DuplicateAdapterError, UnknownAdapterError
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.unit.src.sqltide.adapter._test_types import (
    BuiltinAdapterRegistryTestCase,
    DuplicateAdapterRegistryTestCase,
    ResolveAdapterTestCase,
    UnknownAdapterTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BuiltinAdapterRegistryTestCase(
            description="discovers every built-in adapter by registered name",
            expected_adapter_names=("clickhouse",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_builtin_adapters_when_building_registry_then_it_indexes_every_name(
    test_case: BuiltinAdapterRegistryTestCase,
) -> None:
    registry: dict[str, Adapter] = build_adapter_registry(builtin_adapters())

    assert tuple(sorted(registry)) == test_case.expected_adapter_names


@pytest.mark.parametrize(
    "test_case",
    [
        DuplicateAdapterRegistryTestCase(
            description="rejects two registrations claiming one adapter name",
            registration_count=2,
            expected_message_fragments=(
                "Duplicate adapter name 'clickhouse'",
                "ClickHouseAdapter",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_duplicate_adapter_names_when_building_registry_then_it_rejects_them(
    test_case: DuplicateAdapterRegistryTestCase,
) -> None:
    registrations: tuple[Adapter, ...] = tuple(
        ClickHouseAdapter() for _ in range(test_case.registration_count)
    )

    with pytest.raises(DuplicateAdapterError) as error_info:
        build_adapter_registry(registrations)

    expected_fragment: str
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in str(error_info.value)


@pytest.mark.parametrize(
    "test_case",
    [
        ResolveAdapterTestCase(
            description="resolves the built-in clickhouse adapter",
            adapter_name="clickhouse",
            expected_identity_name="clickhouse",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_registered_adapter_name_when_resolving_then_it_returns_that_adapter(
    test_case: ResolveAdapterTestCase,
) -> None:
    adapter: Adapter = resolve_adapter(test_case.adapter_name)

    assert adapter.identity.name == test_case.expected_identity_name


@pytest.mark.parametrize(
    "test_case",
    [
        UnknownAdapterTestCase(
            description="rejects an unregistered adapter name and lists supported adapters",
            adapter_name="duckdb",
            expected_message_fragments=(
                "Unsupported adapter 'duckdb'",
                "Supported adapters: clickhouse.",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unknown_adapter_name_when_resolving_then_it_raises_before_connecting(
    test_case: UnknownAdapterTestCase,
) -> None:
    with pytest.raises(UnknownAdapterError) as error_info:
        resolve_adapter(test_case.adapter_name)

    expected_fragment: str
    for expected_fragment in test_case.expected_message_fragments:
        assert expected_fragment in str(error_info.value)
