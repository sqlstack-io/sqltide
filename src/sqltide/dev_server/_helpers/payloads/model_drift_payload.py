"""Build live-versus-compiled model drift evidence for the catalog UI."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from sqltide._native import analyze_catalog_sql
from sqltide.adapter.models import (
    AdapterColumn,
    AdapterMaterializedView,
    AdapterTable,
    AdapterView,
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
)
from sqltide.adapters.clickhouse.constants import CLICKHOUSE_DEFAULT_INDEX_GRANULARITY
from sqltide.compiler.compile.models import Column as CompiledColumn
from sqltide.compiler.compile.models import CompiledModel
from sqltide.compiler.pipeline.models import CompileAnalysis

_DRIFT_STATUS: str = "drift"
_IN_SYNC_STATUS: str = "in_sync"
_UNAVAILABLE_STATUS: str = "unavailable"
_TABLE_KIND: str = "table"
_VIEW_KIND: str = "view"


def model_drift_payload(
    *, analysis: CompileAnalysis, model: CompiledModel, catalog: CatalogSnapshot
) -> dict[str, object]:
    """Compare one current realized model with its live warehouse relations."""

    resources: tuple[object, ...] = analysis.realized_project.resources_by_logical_key.get(
        model.key, ()
    )
    desired_relation: AdapterTable | AdapterView | None = next(
        (item for item in resources if isinstance(item, AdapterTable | AdapterView)), None
    )
    desired_writer: AdapterMaterializedView | None = next(
        (item for item in resources if isinstance(item, AdapterMaterializedView)), None
    )
    relations_by_name: dict[str, CatalogRelation] = {
        relation.name: relation for relation in catalog.relations
    }
    live_relation: CatalogRelation | None = (
        None if desired_relation is None else relations_by_name.get(desired_relation.name)
    )
    live_writer: CatalogRelation | None = (
        None if desired_writer is None else relations_by_name.get(desired_writer.name)
    )
    if desired_relation is None or live_relation is None:
        relation_name: str = (
            model.relation_name if desired_relation is None else desired_relation.name
        )
        return _unavailable_payload(relation_name=relation_name)

    schema: dict[str, object] = _schema_payload(
        desired_columns=(
            desired_relation.columns
            if isinstance(desired_relation, AdapterTable)
            else model.output_columns
        ),
        live_columns=live_relation.columns,
    )
    query_resource: AdapterMaterializedView | AdapterView | None = (
        desired_writer
        if desired_writer is not None
        else desired_relation
        if isinstance(desired_relation, AdapterView)
        else None
    )
    desired_query: str = _rendered_query(
        analysis=analysis,
        resource=query_resource,
        database=catalog.identity.database,
    )
    live_query: str | None = (
        live_writer.query_sql
        if desired_writer is not None and live_writer is not None
        else live_relation.query_sql
    )
    query_relation_name: str = (
        desired_writer.name if desired_writer is not None else desired_relation.name
    )
    query: dict[str, object] = _query_payload(
        desired_query=desired_query,
        live_query=live_query,
        relation_name=query_relation_name,
        relation_available=desired_writer is None or live_writer is not None,
        projection_column_names=tuple(column.name for column in model.output_columns),
    )
    physical: dict[str, object] = _physical_payload(
        desired_relation=desired_relation,
        live_relation=live_relation,
    )
    statuses: tuple[object, ...] = (schema["status"], query["status"], physical["status"])
    status: str = (
        _DRIFT_STATUS
        if _DRIFT_STATUS in statuses or _UNAVAILABLE_STATUS in statuses
        else _IN_SYNC_STATUS
    )
    live_ddl_parts: tuple[str, ...] = tuple(
        relation.definition_sql
        for relation in (live_relation, live_writer)
        if relation is not None and relation.definition_sql
    )
    return {
        "comparison": "live_vs_current_compiled",
        "status": status,
        "message": (
            "Live warehouse differs from the current compiled definition."
            if status == _DRIFT_STATUS
            else "Live warehouse matches the current compiled definition."
        ),
        "liveDdl": "\n\n".join(live_ddl_parts) or None,
        "schema": schema,
        "query": query,
        "physicalConfiguration": physical,
    }


def _unavailable_payload(*, relation_name: str) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "status": "unavailable",
        "changes": [],
        "unifiedDiff": None,
    }
    return {
        "comparison": "live_vs_current_compiled",
        "status": "unavailable",
        "message": f"Relation {relation_name} is missing; no live comparison is possible.",
        "liveDdl": None,
        "schema": unavailable,
        "query": {**unavailable, "relationName": relation_name},
        "physicalConfiguration": unavailable,
    }


def _schema_payload(
    *,
    desired_columns: Iterable[AdapterColumn | CompiledColumn],
    live_columns: tuple[CatalogColumn, ...],
) -> dict[str, object]:
    desired: dict[str, tuple[str, str | None]] = {
        str(column.name): (str(column.type), getattr(column, "default_expression", None))
        for column in desired_columns
    }
    live: dict[str, tuple[str, str | None]] = {
        column.name: (column.type, column.default_expression) for column in live_columns
    }
    changes: list[dict[str, object]] = []
    for name in sorted(desired.keys() | live.keys()):
        desired_value: tuple[str, str | None] | None = desired.get(name)
        live_value: tuple[str, str | None] | None = live.get(name)
        if desired_value == live_value:
            continue
        change: str = (
            "added" if live_value is None else "removed" if desired_value is None else "changed"
        )
        changes.append(
            {
                "column": name,
                "live": _column_value(live_value),
                "compiled": _column_value(desired_value),
                "change": change,
            }
        )
    return {"status": _DRIFT_STATUS if changes else _IN_SYNC_STATUS, "changes": changes}


def _column_value(value: tuple[str, str | None] | None) -> str | None:
    if value is None:
        return None
    type_name, default_expression = value
    return type_name if default_expression is None else f"{type_name} DEFAULT {default_expression}"


def _query_payload(
    *,
    desired_query: str,
    live_query: str | None,
    relation_name: str,
    relation_available: bool,
    projection_column_names: tuple[str, ...],
) -> dict[str, object]:
    if not relation_available or live_query is None:
        return {
            "status": "unavailable",
            "relationName": relation_name,
            "live": None,
            "compiled": desired_query,
            "unifiedDiff": None,
        }
    normalized_desired: str = _normalize_projection_coercions(
        query=desired_query,
        column_names=projection_column_names,
    )
    normalized_live: str = _normalize_projection_coercions(
        query=live_query,
        column_names=projection_column_names,
    )
    status: str = _IN_SYNC_STATUS if normalized_desired == normalized_live else _DRIFT_STATUS
    return {
        "status": status,
        "relationName": relation_name,
        "live": live_query,
        "compiled": desired_query,
        "unifiedDiff": (
            None
            if status == _IN_SYNC_STATUS
            else "\n".join(
                difflib.unified_diff(
                    live_query.splitlines(),
                    desired_query.splitlines(),
                    fromfile="live warehouse",
                    tofile="current compiled",
                    lineterm="",
                )
            )
        ),
    }


def _normalize_projection_coercions(*, query: str, column_names: tuple[str, ...]) -> str:
    normalized: str = query
    for column_name in column_names:
        escaped_name: str = re.escape(column_name)
        replacement: str = f"{column_name} AS {column_name}"
        normalized = re.sub(
            rf"CAST\(\s*{escaped_name}\s*,\s*'[^']+'\s*\)\s+AS\s+{escaped_name}",
            replacement,
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            rf"{escaped_name}\s*::\s*[A-Za-z0-9_]+(?:\([^)]*\))?\s+AS\s+{escaped_name}",
            replacement,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def _rendered_query(
    *,
    analysis: CompileAnalysis,
    resource: AdapterMaterializedView | AdapterView | None,
    database: str,
) -> str:
    if resource is None:
        return ""
    definition_sql: str = analysis.adapter_profile.render_resource(
        resource=resource,
        database=database,
    )
    return analyze_catalog_sql(sql=definition_sql, dialect="clickhouse").query_sql or ""


def _physical_payload(
    *, desired_relation: AdapterTable | AdapterView, live_relation: CatalogRelation
) -> dict[str, object]:
    is_table: bool = isinstance(desired_relation, AdapterTable)
    desired_kind: str = _TABLE_KIND if is_table else _VIEW_KIND
    live_kind: str = _VIEW_KIND if live_relation.engine.lower() == _VIEW_KIND else _TABLE_KIND
    desired_engine: str | None = None
    desired_order_by: tuple[str, ...] = ()
    desired_partition_by: str | None = None
    desired_ttl: str | None = None
    desired_settings: tuple[tuple[str, str], ...] = ()
    if isinstance(desired_relation, AdapterTable):
        desired_engine = desired_relation.engine
        desired_order_by = desired_relation.order_by
        desired_partition_by = desired_relation.partition_by
        desired_ttl = desired_relation.ttl
        desired_settings = desired_relation.settings
    live_settings: list[tuple[str, str]] = []
    desired_setting_names: set[str] = {name for name, _ in desired_settings}
    for setting in live_relation.settings:
        if (
            setting == CLICKHOUSE_DEFAULT_INDEX_GRANULARITY
            and setting[0] not in desired_setting_names
        ):
            continue
        live_settings.append(setting)
    desired_fields: tuple[tuple[str, object], ...] = (
        ("Physical identity", desired_relation.name),
        ("Relation kind", desired_kind),
        ("Engine", desired_engine),
        ("ORDER BY", desired_order_by),
        ("PARTITION BY", desired_partition_by),
        ("TTL", desired_ttl),
        ("Settings", desired_settings),
    )
    live_fields: tuple[tuple[str, object], ...] = (
        ("Physical identity", live_relation.name),
        ("Relation kind", live_kind),
        ("Engine", live_relation.storage_engine if is_table else None),
        ("ORDER BY", live_relation.order_by if is_table else ()),
        ("PARTITION BY", live_relation.partition_by if is_table else None),
        ("TTL", live_relation.ttl if is_table else None),
        ("Settings", tuple(live_settings) if is_table else ()),
    )
    changes: list[dict[str, object]] = []
    for (field, desired_value), (_, live_value) in zip(desired_fields, live_fields, strict=True):
        normalized_desired: str = _physical_value(desired_value)
        normalized_live: str = _physical_value(live_value)
        changes.append(
            {
                "field": field,
                "live": normalized_live,
                "compiled": normalized_desired,
                "status": (
                    _IN_SYNC_STATUS if normalized_live == normalized_desired else _DRIFT_STATUS
                ),
            }
        )
    return {
        "status": (
            _DRIFT_STATUS
            if any(item["status"] == _DRIFT_STATUS for item in changes)
            else _IN_SYNC_STATUS
        ),
        "changes": changes,
    }


def _physical_value(value: object) -> str:
    if value is None or value == ():
        return "not configured"
    if isinstance(value, tuple):
        items: list[str] = []
        for item in value:
            if isinstance(item, tuple):
                items.append("=".join(str(part) for part in item))
            else:
                items.append(str(item))
        return ", ".join(items)
    normalized: str = " ".join(str(value).split())
    return normalized.removesuffix("()").lower()
