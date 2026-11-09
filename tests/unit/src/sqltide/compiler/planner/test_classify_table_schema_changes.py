import pytest

from sqltide.compiler.compile.models import (
    Column,
    DesiredTable,
    DesiredView,
    ObjectKey,
    TableSpec,
    TableStorage,
    ViewSpec,
)
from sqltide.compiler.planner.constants import (
    PLANNED_CHANGE_TYPE_NO_OP,
    PLANNED_CHANGE_TYPE_REBUILD,
    TABLE_SCHEMA_CHANGE_KIND_BREAKING,
    TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING,
    TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
    TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
)
from sqltide.compiler.planner.models import (
    ActualTable,
    ActualView,
    PlannedObjectChange,
)
from tests.unit.src.sqltide.compiler.planner._test_types import (
    PlannerTableSchemaClassificationTestCase,
    PlannerViewQueryClassificationTestCase,
)
from tests.unit.src.sqltide.compiler.planner.helpers import planned_change


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerTableSchemaClassificationTestCase(
            description="classifies added column as non-breaking and seedable",
            actual_columns=(("order_id", "UInt64"),),
            desired_columns=(("order_id", "UInt64"), ("region", "String")),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_NON_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="classifies removed column as breaking and seedable",
            actual_columns=(("order_id", "UInt64"), ("region", "String")),
            desired_columns=(("order_id", "UInt64"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="classifies type change as breaking and non-seedable",
            actual_columns=(("order_id", "UInt64"),),
            desired_columns=(("order_id", "String"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="classifies add and remove combination as breaking and seedable",
            actual_columns=(("order_id", "UInt64"), ("region", "String")),
            desired_columns=(("order_id", "UInt64"), ("country", "String")),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats equivalent clickhouse type casing as no-op",
            actual_columns=(("observed_day", "Date"),),
            desired_columns=(("observed_day", "DATE"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats decimal spacing differences as no-op",
            actual_columns=(("order_total", "Decimal(18,2)"),),
            desired_columns=(("order_total", "Decimal(18, 2)"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats nested nullable low cardinality casing as no-op",
            actual_columns=(("region", "Nullable(LowCardinality(String))"),),
            desired_columns=(("region", "nullable(lowcardinality(string))"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats datetime precision spacing as no-op",
            actual_columns=(("observed_at", "DateTime64(3)"),),
            desired_columns=(("observed_at", "DateTime64( 3 )"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats array inner type casing as no-op",
            actual_columns=(("headers", "Array(String)"),),
            desired_columns=(("headers", "array(string)"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="still treats real nested type changes as breaking",
            actual_columns=(("region", "Nullable(String)"),),
            desired_columns=(("region", "Nullable(FixedString(2))"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="treats nested map and array type casing as no-op",
            actual_columns=(("attributes", "Map(String, Array(UInt32))"),),
            desired_columns=(("attributes", "map(string, array(uint32))"),),
            expected_schema_change_kind=None,
            expected_seed_compatibility=None,
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves case-sensitive enum labels",
            actual_columns=(("state", "Enum8('ready' = 1)"),),
            desired_columns=(("state", "Enum8('READY' = 1)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves case-sensitive datetime timezone names",
            actual_columns=(("observed_at", "DateTime64(3, 'UTC')"),),
            desired_columns=(("observed_at", "DateTime64(3, 'utc')"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves named tuple field casing",
            actual_columns=(("payload", "Tuple(UserID UInt64)"),),
            desired_columns=(("payload", "Tuple(userid UInt64)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves named tuple fields that collide with type vocabulary",
            actual_columns=(("payload", "Tuple(date UInt64, map String)"),),
            desired_columns=(("payload", "Tuple(Date UInt64, Map String)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves nested fields that collide with type vocabulary",
            actual_columns=(("payload", "Nested(string String, date Date)"),),
            desired_columns=(("payload", "Nested(String String, Date Date)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves aggregate function name casing",
            actual_columns=(("payload", "AggregateFunction(map, UInt64)"),),
            desired_columns=(("payload", "AggregateFunction(Map, UInt64)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
        PlannerTableSchemaClassificationTestCase(
            description="preserves simple aggregate function name casing",
            actual_columns=(("payload", "SimpleAggregateFunction(map, UInt64)"),),
            desired_columns=(("payload", "SimpleAggregateFunction(Map, UInt64)"),),
            expected_schema_change_kind=TABLE_SCHEMA_CHANGE_KIND_BREAKING,
            expected_seed_compatibility=TABLE_SCHEMA_SEED_COMPATIBILITY_NON_SEEDABLE,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_table_column_changes_when_classifying_then_it_returns_expected_schema_metadata(
    test_case: PlannerTableSchemaClassificationTestCase,
) -> None:
    actual_table: ActualTable = ActualTable(
        key=ObjectKey(None, "table", "tbl__orders_enriched"),
        spec=TableSpec(
            columns=tuple(Column(name, type_name) for name, type_name in test_case.actual_columns),
            storage=TableStorage(engine="MergeTree()", order_by=("order_id",)),
        ),
    )
    desired_table: DesiredTable = DesiredTable(
        key=ObjectKey(None, "table", "tbl__orders_enriched"),
        deps=(),
        spec=TableSpec(
            columns=tuple(Column(name, type_name) for name, type_name in test_case.desired_columns),
            storage=TableStorage(engine="MergeTree()", order_by=("order_id",)),
        ),
    )

    change: PlannedObjectChange = planned_change(
        desired_object=desired_table, actual_object=actual_table
    )

    assert change.change_type == test_case.expected_change_type
    assert change.schema_change_kind == test_case.expected_schema_change_kind
    assert change.seed_compatibility == test_case.expected_seed_compatibility


@pytest.mark.parametrize(
    "test_case",
    [
        PlannerViewQueryClassificationTestCase(
            description="treats a reformatted deployed query as unchanged",
            desired_query="SELECT CAST(order_id AS UInt64) AS order_id FROM raw__orders",
            actual_query=("SELECT\n  CAST(order_id AS UInt64) AS order_id\nFROM raw__orders"),
            expected_change_type=PLANNED_CHANGE_TYPE_NO_OP,
        ),
        PlannerViewQueryClassificationTestCase(
            description="treats a genuinely different deployed query as a change",
            desired_query="SELECT CAST(order_id AS UInt64) AS order_id FROM raw__orders",
            actual_query="SELECT CAST(order_id AS UInt64) AS order_id FROM raw__invoices",
            expected_change_type=PLANNED_CHANGE_TYPE_REBUILD,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deployed_view_query_when_classifying_then_formatting_is_not_a_change(
    test_case: PlannerViewQueryClassificationTestCase,
) -> None:
    key: ObjectKey = ObjectKey(name="vw__orders", object_type="view", database=None)
    desired_view: DesiredView = DesiredView(
        key=key,
        deps=(),
        spec=ViewSpec(query=test_case.desired_query, database_template=test_case.desired_query),
    )
    actual_view: ActualView = ActualView(
        key=key,
        spec=ViewSpec(query=test_case.actual_query, database_template=test_case.actual_query),
    )

    assert (
        planned_change(desired_object=desired_view, actual_object=actual_view).change_type
        == test_case.expected_change_type
    )
