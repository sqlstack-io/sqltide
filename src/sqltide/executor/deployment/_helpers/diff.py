"""Resolve deployment diff endpoints and compare their live physical relations."""

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE
from sqltide.adapter.models import (
    AdapterDeploymentInventory,
    AdapterDeploymentRecord,
    AdapterQueryResult,
    CatalogRelation,
    CatalogSnapshot,
    InspectedManagedTableState,
)
from sqltide.compiler.compile.constants import (
    DESIRED_OBJECT_TYPE_TABLE,
    DESIRED_OBJECT_TYPE_VIEW,
)
from sqltide.executor.deployment.exceptions import DeploymentDiffError
from sqltide.executor.deployment.models import (
    DeploymentDiffColumn,
    DeploymentDiffRelation,
    DeploymentDiffRequest,
    DeploymentDiffResult,
)
from sqltide.executor.deployment.types import DeploymentDiffStatus


def execute_diff(
    *, request: DeploymentDiffRequest, client: AdapterConnection
) -> DeploymentDiffResult:
    """Resolve both endpoints and compare schemas and row counts."""

    try:
        from_endpoint, to_endpoint = _native.parse_deployment_diff(
            request.comparison, [request.database, request.metadata_database]
        )
    except ValueError as error:
        raise DeploymentDiffError(str(error)) from None
    inventory: AdapterDeploymentInventory = client.load_deployment_inventory(
        request.metadata_database
    )
    managed_state: InspectedManagedTableState = client.inspect_managed_table_state(request.database)
    try:
        pairs, databases = _native.deployment_diff_relations(
            from_endpoint,
            to_endpoint,
            [_native_deployment(deployment) for deployment in inventory.deployments],
            [
                (binding.database, binding.logical_name, binding.physical_name)
                for binding in managed_state.active_bindings
            ],
            request.database,
        )
    except ValueError as error:
        raise DeploymentDiffError(str(error)) from None
    catalogs: dict[str, CatalogSnapshot] = {
        database: client.load_catalog(database) for database in databases
    }
    return DeploymentDiffResult(
        database=request.database,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        relations=tuple(
            _compare_relation(
                database=database,
                logical_name=logical_name,
                from_physical_name=from_physical_name,
                to_physical_name=to_physical_name,
                catalog=catalogs[database],
                client=client,
            )
            for database, logical_name, from_physical_name, to_physical_name in pairs
        ),
    )


def _native_deployment(
    deployment: AdapterDeploymentRecord,
) -> tuple[str, str, bool, list[tuple[str | None, bool, str, str, str]]]:
    return (
        deployment.deployment_id,
        deployment.created_at,
        deployment.status == VIRTUAL_DEPLOYMENT_STATUS_INCOMPLETE,
        [
            (
                mapping.logical_key.database,
                mapping.logical_key.object_type
                in {DESIRED_OBJECT_TYPE_TABLE, DESIRED_OBJECT_TYPE_VIEW},
                mapping.logical_key.name,
                mapping.physical_name,
                mapping.logical_model_name,
            )
            for mapping in deployment.prepared_object_mappings
        ],
    )


def _compare_relation(
    *,
    database: str,
    logical_name: str,
    from_physical_name: str | None,
    to_physical_name: str | None,
    catalog: CatalogSnapshot,
    client: AdapterConnection,
) -> DeploymentDiffRelation:
    from_catalog: CatalogRelation | None = (
        None if from_physical_name is None else catalog.relation(from_physical_name)
    )
    same_physical_relation: bool = (
        from_physical_name is not None and from_physical_name == to_physical_name
    )
    to_catalog: CatalogRelation | None = None
    if same_physical_relation:
        to_catalog = from_catalog
    elif to_physical_name is not None:
        to_catalog = catalog.relation(to_physical_name)
    from_columns: tuple[DeploymentDiffColumn, ...] = _diff_columns(from_catalog)
    to_columns: tuple[DeploymentDiffColumn, ...] = _diff_columns(to_catalog)
    from_row_count: int | None = _row_count(relation=from_catalog, database=database, client=client)
    to_row_count: int | None = (
        from_row_count
        if same_physical_relation
        else _row_count(relation=to_catalog, database=database, client=client)
    )
    return DeploymentDiffRelation(
        database=database,
        logical_name=logical_name,
        status=DeploymentDiffStatus(
            _native.deployment_diff_status(
                _native_side(
                    physical_name=from_physical_name,
                    relation=from_catalog,
                    columns=from_columns,
                    row_count=from_row_count,
                ),
                _native_side(
                    physical_name=to_physical_name,
                    relation=to_catalog,
                    columns=to_columns,
                    row_count=to_row_count,
                ),
            )
        ),
        from_physical_name=from_physical_name,
        to_physical_name=to_physical_name,
        from_columns=from_columns,
        to_columns=to_columns,
        from_row_count=from_row_count,
        to_row_count=to_row_count,
    )


def _native_side(
    *,
    physical_name: str | None,
    relation: CatalogRelation | None,
    columns: tuple[DeploymentDiffColumn, ...],
    row_count: int | None,
) -> tuple[bool, tuple[list[tuple[str, str, str | None]], int | None] | None]:
    return (
        physical_name is not None,
        None
        if relation is None
        else (
            [(column.name, column.type, column.default_expression) for column in columns],
            row_count,
        ),
    )


def _diff_columns(relation: CatalogRelation | None) -> tuple[DeploymentDiffColumn, ...]:
    if relation is None:
        return ()
    return tuple(
        DeploymentDiffColumn(
            name=column.name,
            type=column.type,
            default_expression=column.default_expression,
        )
        for column in relation.columns
    )


def _row_count(
    *, relation: CatalogRelation | None, database: str, client: AdapterConnection
) -> int | None:
    if relation is None:
        return None
    try:
        sql: str = _native.deployment_diff_count_sql(database, relation.name)
    except ValueError as error:
        raise DeploymentDiffError(str(error)) from None
    result: AdapterQueryResult = client.query(sql)
    if len(result.rows) != 1 or len(result.rows[0]) != 1:
        raise DeploymentDiffError(
            f"Deployment diff count for '{relation.name}' returned an unexpected shape"
        )
    value: object = result.rows[0][0]
    if not isinstance(value, int):
        raise DeploymentDiffError(f"Deployment diff count for '{relation.name}' was not an integer")
    return value
