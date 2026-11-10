"""Render scheduled postgres refresh relations as ClickHouse DDL."""

import pytest

from sqltide._native import render_clickhouse_resource
from sqltide.adapter.constants import (
    ADAPTER_SECRET_PLACEHOLDER_PREFIX,
    ADAPTER_SECRET_PLACEHOLDER_SUFFIX,
)
from sqltide.adapter.exceptions import AdapterCapabilityError
from sqltide.adapter.models import AdapterMaterializedView
from tests.unit.src.sqltide.adapters.clickhouse._test_types import (
    MissingSecretTestCase,
    RefreshRenderingTestCase,
    SecretRenderingTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RefreshRenderingTestCase(
            description="an appending refresh renders its cadence and target",
            refresh="1 HOUR",
            append=True,
            expected_fragments=(
                "REFRESH EVERY 1 HOUR APPEND",
                "TO analytics.pg__course AS",
                "SETTINGS allow_experimental_refreshable_materialized_view = 1",
            ),
            forbidden_fragments=(),
        ),
        RefreshRenderingTestCase(
            description="replace semantics omit the append clause",
            refresh="5 MINUTE",
            append=False,
            expected_fragments=(
                "REFRESH EVERY 5 MINUTE\n",
                "SETTINGS allow_experimental_refreshable_materialized_view = 1",
            ),
            forbidden_fragments=("APPEND",),
        ),
        RefreshRenderingTestCase(
            description="a view without a cadence stays incremental",
            refresh=None,
            append=True,
            expected_fragments=("TO analytics.pg__course AS",),
            forbidden_fragments=("REFRESH",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_refresh_cadence_when_rendering_then_ddl_matches_the_declaration(
    test_case: RefreshRenderingTestCase,
) -> None:
    resource: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__pg__course",
        source_relation_name="inventory__course",
        target_relation_name="pg__course",
        query="SELECT 1",
        database_template="SELECT 1",
        refresh=test_case.refresh,
        append=test_case.append,
    )

    ddl: str = render_clickhouse_resource(resource=resource, database="analytics")

    fragment: str
    for fragment in test_case.expected_fragments:
        assert fragment in ddl
    for fragment in test_case.forbidden_fragments:
        assert fragment not in ddl


@pytest.mark.parametrize(
    "test_case",
    [
        SecretRenderingTestCase(
            description="a credential placeholder resolves from the environment",
            environment=(("INVENTORY_READONLY_PASSWORD", "s3cret"),),
            variable_name="INVENTORY_READONLY_PASSWORD",
            expected_fragment="s3cret",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_credential_placeholder_when_rendering_then_the_secret_is_resolved(
    test_case: SecretRenderingTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name: str
    value: str
    for name, value in test_case.environment:
        monkeypatch.setenv(name, value)
    placeholder: str = (
        f"{ADAPTER_SECRET_PLACEHOLDER_PREFIX}"
        f"{test_case.variable_name}"
        f"{ADAPTER_SECRET_PLACEHOLDER_SUFFIX}"
    )
    query: str = (
        f"SELECT * FROM postgresql('pg:5432', 'inventory', 'course', 'ro', '{placeholder}')"
    )
    resource: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__pg__course",
        source_relation_name="inventory__course",
        target_relation_name="pg__course",
        query=query,
        database_template=query,
        refresh="1 HOUR",
    )

    ddl: str = render_clickhouse_resource(resource=resource, database="analytics")

    assert test_case.expected_fragment in ddl
    assert ADAPTER_SECRET_PLACEHOLDER_PREFIX not in ddl


@pytest.mark.parametrize(
    "test_case",
    [
        MissingSecretTestCase(
            description="an unset credential names the variable it needs",
            variable_name="INVENTORY_READONLY_PASSWORD",
            expected_error_fragment="INVENTORY_READONLY_PASSWORD",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_credential_when_rendering_then_it_reports_the_variable(
    test_case: MissingSecretTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(test_case.variable_name, raising=False)
    placeholder: str = (
        f"{ADAPTER_SECRET_PLACEHOLDER_PREFIX}"
        f"{test_case.variable_name}"
        f"{ADAPTER_SECRET_PLACEHOLDER_SUFFIX}"
    )
    resource: AdapterMaterializedView = AdapterMaterializedView(
        name="mv__pg__course",
        source_relation_name="inventory__course",
        target_relation_name="pg__course",
        query=f"SELECT '{placeholder}'",
        database_template=f"SELECT '{placeholder}'",
        refresh="1 HOUR",
    )

    with pytest.raises(AdapterCapabilityError, match=test_case.expected_error_fragment):
        render_clickhouse_resource(resource=resource, database="analytics")
