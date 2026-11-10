"""ClickHouse implementation of the neutral adapter contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError, StreamFailureError

from sqltide._native import (
    build_clickhouse_connection_config,
    realize_clickhouse_model,
    realize_clickhouse_source,
    render_clickhouse_resource,
    render_clickhouse_set_difference_comparison,
    translate_clickhouse_driver_error,
)
from sqltide.adapter.classes.adapter import Adapter
from sqltide.adapter.models import (
    AdapterAdoptedSourceRealizationRequest,
    AdapterConnectionConfig,
    AdapterIdentity,
    AdapterManagedSource,
    AdapterManagedSourceRealizationRequest,
    AdapterMaterializedView,
    AdapterModelRealization,
    AdapterModelRealizationRequest,
    AdapterSetDifferenceComparisonRequest,
    AdapterSourceRealization,
    AdapterStableView,
    AdapterTable,
    AdapterView,
    AdapterViewRealizationRequest,
)
from sqltide.adapters.clickhouse.classes.clickhouse_connection import ClickHouseConnection
from sqltide.adapters.clickhouse.constants import (
    CLICKHOUSE_ADAPTER_NAME,
    CLICKHOUSE_DEFAULT_DATABASE,
    CLICKHOUSE_MODEL_TABLE_NAME_PREFIX,
    CLICKHOUSE_SQL_ANALYSIS_DIALECT,
)
from sqltide.adapters.clickhouse.types import RawClickHouseClient


class ClickHouseAdapter(Adapter):
    """The built-in ClickHouse adapter."""

    @property
    def identity(self) -> AdapterIdentity:
        """Return the registered ClickHouse adapter identity."""

        return AdapterIdentity(name=CLICKHOUSE_ADAPTER_NAME)

    @property
    def sql_analysis_dialect(self) -> str:
        """Return the mandatory Polyglot dialect name."""

        return CLICKHOUSE_SQL_ANALYSIS_DIALECT

    @property
    def default_database(self) -> str:
        """Return ClickHouse's default database name."""

        return CLICKHOUSE_DEFAULT_DATABASE

    @property
    def default_schema(self) -> None:
        """Return no schema because ClickHouse uses database-qualified relations."""

        return None

    def connect(self, config: AdapterConnectionConfig) -> ClickHouseConnection:
        """Open a ClickHouse connection for the resolved configuration."""

        try:
            raw_client: RawClickHouseClient = self._open_raw_client(config)
        except (ClickHouseError, StreamFailureError) as error:
            raise translate_clickhouse_driver_error(error) from error
        return ClickHouseConnection(raw_client)

    def build_connection_config(
        self,
        *,
        values: Mapping[str, object],
        database: str | None,
    ) -> AdapterConnectionConfig:
        """Validate the exercised ClickHouse connection fields without opening a connection."""

        return build_clickhouse_connection_config(values=values, database=database)

    def render_resource(
        self,
        *,
        resource: (
            AdapterManagedSource
            | AdapterTable
            | AdapterMaterializedView
            | AdapterView
            | AdapterStableView
        ),
        database: str,
        if_not_exists: bool = False,
    ) -> str:
        """Render one neutral resource request as ClickHouse SQL."""

        return render_clickhouse_resource(
            resource=resource,
            database=database,
            if_not_exists=if_not_exists,
        )

    def realize_source(
        self,
        *,
        request: AdapterManagedSourceRealizationRequest | AdapterAdoptedSourceRealizationRequest,
    ) -> AdapterSourceRealization:
        """Map one logical source to ClickHouse resources."""

        return realize_clickhouse_source(request=request)

    def model_relation_name(self, *, logical_name: str) -> str:
        """Resolve the ClickHouse table name for one logical model."""

        return f"{CLICKHOUSE_MODEL_TABLE_NAME_PREFIX}{logical_name}"

    def realize_model(
        self, *, request: AdapterModelRealizationRequest | AdapterViewRealizationRequest
    ) -> AdapterModelRealization:
        """Map one semantically compiled model to ClickHouse resources."""

        return realize_clickhouse_model(request=request)

    def render_set_difference_comparison(
        self, *, request: AdapterSetDifferenceComparisonRequest
    ) -> str:
        """Render neutral bag-comparison inputs as one ClickHouse query."""

        return render_clickhouse_set_difference_comparison(request.targets)

    def _open_raw_client(self, config: AdapterConnectionConfig) -> RawClickHouseClient:
        """Open a session-less driver client; session ids only cause SESSION_IS_LOCKED here."""

        if config.database is None:
            return cast(
                RawClickHouseClient,
                clickhouse_connect.get_client(
                    host=config.host,
                    port=config.port,
                    username=config.username,
                    password=config.password,
                    autogenerate_session_id=False,
                    settings=dict(config.settings),
                ),
            )
        return cast(
            RawClickHouseClient,
            clickhouse_connect.get_client(
                host=config.host,
                port=config.port,
                username=config.username,
                password=config.password,
                database=config.database,
                autogenerate_session_id=False,
                settings=dict(config.settings),
            ),
        )
