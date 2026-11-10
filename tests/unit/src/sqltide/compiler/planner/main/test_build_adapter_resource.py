import pytest

from sqltide._native import (
    build_adapter_resource,
    build_shadow_adapter_resource,
)
from sqltide.adapter.models import AdapterMaterializedView
from sqltide.compiler.compile.constants import DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW
from sqltide.compiler.compile.models import (
    DesiredMaterializedView,
    MaterializedViewSpec,
    ObjectKey,
)
from tests.unit.src.sqltide.compiler.planner.main._test_types import (
    MaterializedViewSchedulingTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        MaterializedViewSchedulingTestCase(
            description="preserves appending refresh scheduling",
            expected_refresh="15 MINUTE",
            expected_append=True,
        ),
        MaterializedViewSchedulingTestCase(
            description="preserves replacing refresh scheduling",
            expected_refresh="1 HOUR",
            expected_append=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_scheduled_view_when_building_adapter_resources_then_scheduling_is_retained(
    test_case: MaterializedViewSchedulingTestCase,
) -> None:
    key: ObjectKey = ObjectKey(
        database=None,
        object_type=DESIRED_OBJECT_TYPE_MATERIALIZED_VIEW,
        name="mv__pg__course",
    )
    desired: DesiredMaterializedView = DesiredMaterializedView(
        key=key,
        deps=(),
        spec=MaterializedViewSpec(
            source_table_name="inventory__course",
            target_table_name="pg__course",
            query='SELECT * FROM postgresql("pg", "db", "course", "ro", "")',
            database_template='SELECT * FROM postgresql("pg", "db", "course", "ro", "")',
        ),
        refresh=test_case.expected_refresh,
        append=test_case.expected_append,
    )

    direct: object = build_adapter_resource(desired)
    shadow: object = build_shadow_adapter_resource(
        desired_object=desired,
        physical_name="mv__pg__course__deployment",
        physical_name_by_key={key: "mv__pg__course__deployment"},
    )

    assert isinstance(direct, AdapterMaterializedView)
    assert direct.refresh == test_case.expected_refresh
    assert direct.append is test_case.expected_append
    assert isinstance(shadow, AdapterMaterializedView)
    assert shadow.refresh == test_case.expected_refresh
    assert shadow.append is test_case.expected_append
