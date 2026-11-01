"""Migrate and persist SQLTide metadata in ClickHouse."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import (
    METADATA_DIRECT_FINGERPRINTS_TABLE_NAME,
    METADATA_MANIFESTS_TABLE_NAME,
)
from sqltide.adapter.exceptions import AdapterResultError, AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterCurrentQualityNode,
    AdapterDirectFingerprintRecord,
    AdapterDirectFingerprintSnapshot,
    AdapterManifest,
    AdapterManifestResource,
    AdapterManifestSnapshot,
    AdapterMetadataState,
    AdapterQueryResult,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
    AdapterSensorState,
)
from sqltide.adapter.types import AdapterOptionalStateStatus

_DIRECT_FINGERPRINT_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "fingerprint_id",
        "logical_model_identity",
        "definition_sql",
        "definition_hash",
        "identity_metadata",
        "workflow_id",
        "tool_version",
        "applied_at",
    }
)
_MANIFEST_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "manifest_id",
        "invocation_id",
        "project_identity",
        "target_name",
        "target_database",
        "is_production",
        "project_revision",
        "manifest_fingerprint",
        "manifest_version",
        "pipelines",
        "resources",
        "tool_version",
        "published_at",
    }
)


def render_clickhouse_metadata_migration_statements(database: str) -> tuple[str, ...]:
    """Render the current additive ClickHouse metadata migration."""

    return tuple(_native.render_clickhouse_metadata_migration_statements(database))


def render_clickhouse_metadata_migration_workflow(database: str) -> tuple[str, ...]:
    """Render the complete idempotent metadata migration as executable SQL."""

    return tuple(_native.render_clickhouse_metadata_migration_workflow(database))


def render_clickhouse_direct_fingerprint_observations(
    *, database: str, fingerprints: tuple[AdapterDirectFingerprintRecord, ...]
) -> tuple[str, ...]:
    """Render best-effort schema initialization and logical baseline inserts."""

    return tuple(_native.render_clickhouse_direct_fingerprint_observations(database, fingerprints))


def render_clickhouse_metadata_state(
    *, database: str, state: AdapterMetadataState
) -> tuple[str, ...]:
    """Render metadata rows as exact manually executable ClickHouse inserts."""

    return tuple(_native.render_clickhouse_metadata_state(database, state))


def render_clickhouse_manifest_publication(
    *, database: str, manifest: AdapterManifest
) -> tuple[str, ...]:
    """Render one append containing a complete immutable manifest."""

    return (_native.render_clickhouse_manifest_publication(database, manifest),)


def render_clickhouse_sensor_state_inserts(
    *,
    database: str,
    state: AdapterSensorState,
    include_migration: bool = False,
) -> tuple[str, ...]:
    """Render incremental sensor-state inserts, optionally preceded by the migration."""

    return tuple(_native.render_clickhouse_sensor_state_inserts(database, state, include_migration))


def render_clickhouse_sensor_retention_cleanup(
    *, database: str, retention_days: int
) -> tuple[str, ...]:
    """Render bounded deletes for aged sensor ticks and steps; zero keeps everything."""

    return tuple(_native.render_clickhouse_sensor_retention_cleanup(database, retention_days))


def render_clickhouse_run_event_inserts(
    *,
    database: str,
    events: tuple[AdapterRunEventRecord, ...],
    include_migration: bool = False,
) -> tuple[str, ...]:
    """Render incremental run-event inserts, optionally preceded by the migration."""

    return tuple(_native.render_clickhouse_run_event_inserts(database, events, include_migration))


def render_clickhouse_run_statement_inserts(
    *,
    database: str,
    statements: tuple[AdapterRunStatementRecord, ...],
    include_migration: bool = False,
) -> tuple[str, ...]:
    """Render one complete run statement set before warehouse execution."""

    return tuple(
        _native.render_clickhouse_run_statement_inserts(database, statements, include_migration)
    )


def render_clickhouse_latest_node_status_query(
    *,
    database: str,
    project_identity: str,
    target_identity: str,
    nodes: tuple[AdapterCurrentQualityNode, ...],
) -> str:
    """Join current manifest fingerprints to latest terminal result history."""

    return _native.render_clickhouse_latest_node_status_query(
        database, project_identity, target_identity, nodes
    )


def load_clickhouse_direct_fingerprints(
    *,
    connection: AdapterConnection,
    database: str,
    logical_model_identities: tuple[str, ...],
) -> AdapterDirectFingerprintSnapshot:
    """Load latest compatible baselines without making them execution state."""

    try:
        columns: frozenset[str] = connection.metadata_columns(
            database=database,
            table=METADATA_DIRECT_FINGERPRINTS_TABLE_NAME,
        )
        if not columns:
            return AdapterDirectFingerprintSnapshot(
                status=AdapterOptionalStateStatus.ABSENT,
                baselines=(),
            )
        if not _DIRECT_FINGERPRINT_REQUIRED_COLUMNS <= columns:
            return AdapterDirectFingerprintSnapshot(
                status=AdapterOptionalStateStatus.UNAVAILABLE,
                baselines=(),
                warning="Direct SQL baseline table has an incompatible schema",
            )
        if not logical_model_identities:
            return AdapterDirectFingerprintSnapshot(
                status=AdapterOptionalStateStatus.AVAILABLE,
                baselines=(),
            )
        result: AdapterQueryResult = connection.query(
            _native.render_clickhouse_latest_fingerprints_query(
                database, list(logical_model_identities)
            )
        )
    except AdapterWarehouseError as error:
        return AdapterDirectFingerprintSnapshot(
            status=AdapterOptionalStateStatus.UNAVAILABLE,
            baselines=(),
            warning=f"Direct SQL baselines unavailable: {error}",
        )
    return AdapterDirectFingerprintSnapshot(
        status=AdapterOptionalStateStatus.AVAILABLE,
        baselines=tuple(
            AdapterDirectFingerprintRecord(
                fingerprint_id=str(row[0]),
                logical_model_identity=str(row[1]),
                definition_sql=str(row[2]),
                definition_hash=str(row[3]),
                identity_metadata=str(row[4]),
                workflow_id=str(row[5]),
                tool_version=str(row[6]),
                applied_at=str(row[7]),
            )
            for row in result.rows
        ),
    )


def load_clickhouse_manifests(
    *,
    connection: AdapterConnection,
    database: str,
    project_identity: str,
    target_name: str,
    target_database: str,
) -> AdapterManifestSnapshot:
    """Load complete manifest history for one project target, newest first."""

    try:
        columns: frozenset[str] = connection.metadata_columns(
            database=database,
            table=METADATA_MANIFESTS_TABLE_NAME,
        )
        if not columns:
            return AdapterManifestSnapshot(status="absent", manifests=())
        if not _MANIFEST_REQUIRED_COLUMNS <= columns:
            return AdapterManifestSnapshot(
                status="unavailable",
                manifests=(),
                warning="Manifest table has an incompatible schema",
            )
        result: AdapterQueryResult = connection.query(
            _native.render_clickhouse_manifest_history_query(
                database, project_identity, target_name, target_database
            )
        )
    except AdapterWarehouseError as error:
        return AdapterManifestSnapshot(
            status="unavailable",
            manifests=(),
            warning=f"Manifest history unavailable: {error}",
        )
    manifests: tuple[AdapterManifest, ...] = tuple(_manifest_from_row(row) for row in result.rows)
    return AdapterManifestSnapshot(status="available", manifests=manifests)


def _manifest_from_row(row: tuple[object, ...]) -> AdapterManifest:
    pipelines: tuple[str, ...] = tuple(str(pipeline) for pipeline in cast(Sequence[object], row[9]))
    resources: tuple[AdapterManifestResource, ...] = tuple(
        _manifest_resource(resource) for resource in cast(Sequence[object], row[10])
    )
    project_revision: str | None = None if row[6] is None else str(row[6])
    return AdapterManifest(
        manifest_id=str(row[0]),
        invocation_id=str(row[1]),
        project_identity=str(row[2]),
        target_name=str(row[3]),
        target_database=str(row[4]),
        is_production=bool(row[5]),
        project_revision=project_revision,
        manifest_fingerprint=str(row[7]),
        manifest_version=int(cast(int, row[8])),
        pipelines=pipelines,
        resources=resources,
        tool_version=str(row[11]),
        published_at=str(row[12]),
    )


def _manifest_resource(resource: object) -> AdapterManifestResource:
    names: tuple[str, ...] = (
        "pipeline_name",
        "logical_type",
        "logical_name",
        "resource_role",
        "resource_database",
        "resource_name",
        "resource_kind",
    )
    if isinstance(resource, Mapping):
        mapping: Mapping[str, object] = cast(Mapping[str, object], resource)
        values: tuple[object, ...] = tuple(mapping[name] for name in names)
    elif isinstance(resource, Sequence) and not isinstance(resource, str | bytes):
        values = tuple(resource)
    else:
        raise AdapterResultError("Manifest resource row is not a tuple")
    if len(values) != len(names):
        raise AdapterResultError("Manifest resource row has an incompatible shape")
    return AdapterManifestResource(
        pipeline_name=str(values[0]),
        logical_type=str(values[1]),
        logical_name=str(values[2]),
        resource_role=str(values[3]),
        resource_database=str(values[4]),
        resource_name=str(values[5]),
        resource_kind=str(values[6]),
    )
