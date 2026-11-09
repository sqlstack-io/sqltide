"""Build neutral adapter resource requests from desired objects."""

from sqltide.adapter.constants import MANAGED_SOURCE_KIND_KAFKA
from sqltide.adapter.models import (
    AdapterColumn,
    AdapterManagedSource,
    AdapterMaterializedView,
    AdapterStableView,
    AdapterTable,
    AdapterView,
)
from sqltide.compiler.compile.models import (
    Column,
    DesiredKafkaTable,
    DesiredMaterializedView,
    DesiredTable,
    DesiredView,
)
from sqltide.compiler.planner.exceptions import DeploymentPlanError


def build_adapter_resource(
    desired_object: DesiredKafkaTable | DesiredTable | DesiredMaterializedView | DesiredView,
) -> (
    AdapterManagedSource | AdapterTable | AdapterMaterializedView | AdapterView | AdapterStableView
):
    """Convert one desired object into a neutral adapter resource request."""

    columns: tuple[AdapterColumn, ...] = (
        tuple(_adapter_column(column) for column in desired_object.spec.columns)
        if isinstance(desired_object, (DesiredKafkaTable, DesiredTable))
        else ()
    )
    if isinstance(desired_object, DesiredKafkaTable):
        return AdapterManagedSource(
            source_kind=MANAGED_SOURCE_KIND_KAFKA,
            name=desired_object.name,
            columns=columns,
            broker_list=desired_object.kafka.broker_list,
            topic=desired_object.kafka.topic,
            consumer_group=desired_object.kafka.consumer_group,
            format=desired_object.kafka.format,
            settings=tuple(sorted((desired_object.kafka.settings or {}).items())),
            naming_macro_fingerprint=desired_object.spec.naming_macro_fingerprint,
        )
    if isinstance(desired_object, DesiredTable):
        return AdapterTable(
            name=desired_object.name,
            columns=columns,
            engine=desired_object.engine,
            order_by=desired_object.order_by,
            partition_by=desired_object.partition_by,
            ttl=desired_object.ttl,
            settings=tuple(sorted((desired_object.settings or {}).items())),
        )
    if isinstance(desired_object, DesiredMaterializedView):
        return AdapterMaterializedView(
            name=desired_object.name,
            source_relation_name=desired_object.source_table_name,
            target_relation_name=desired_object.target_table_name,
            query=desired_object.query,
            database_template=_database_template(desired_object),
            refresh=desired_object.refresh,
            append=desired_object.append,
            dead_letters=desired_object.dead_letters,
        )
    return AdapterView(
        name=desired_object.name,
        query=desired_object.query,
        database_template=_database_template(desired_object),
    )


def _adapter_column(column: Column) -> AdapterColumn:
    return AdapterColumn(
        name=column.name,
        type=column.type,
        default_expression=column.default,
    )


def _database_template(desired_object: DesiredMaterializedView | DesiredView) -> str:
    database_template: str | None = desired_object.spec.database_template
    if database_template is None:
        raise DeploymentPlanError(
            f"View '{desired_object.name}' lacks an analyzed database template"
        )
    return database_template
