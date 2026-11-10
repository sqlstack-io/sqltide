from typing import cast

import pytest

from sqltide._native import deployment_diff_payload
from sqltide.executor.deployment.models import (
    DeploymentDiffColumn,
    DeploymentDiffRelation,
    DeploymentDiffResult,
)
from tests.unit.src.sqltide.dev_server._test_types import DeploymentDiffPayloadTestCase

_ORDER_ID: DeploymentDiffColumn = DeploymentDiffColumn(
    name="order_id", type="String", default_expression=None
)
_TOTAL: DeploymentDiffColumn = DeploymentDiffColumn(
    name="total", type="Float64", default_expression=None
)
_REGION: DeploymentDiffColumn = DeploymentDiffColumn(
    name="region", type="String", default_expression=None
)

_RESULT: DeploymentDiffResult = DeploymentDiffResult(
    database="analytics",
    from_endpoint="active",
    to_endpoint="20260410T005500Z_cd34ef",
    relations=(
        DeploymentDiffRelation(
            database="analytics",
            logical_name="tbl__orders",
            status="changed",
            from_physical_name="tbl__orders__a1b2cd",
            to_physical_name="tbl__orders__cd34ef",
            from_columns=(_ORDER_ID, _TOTAL),
            to_columns=(_ORDER_ID, _REGION),
            from_row_count=1000,
            to_row_count=1200,
        ),
        DeploymentDiffRelation(
            database="analytics",
            logical_name="tbl__revenue",
            status="unchanged",
            from_physical_name="tbl__revenue__a1b2cd",
            to_physical_name="tbl__revenue__cd34ef",
            from_columns=(_ORDER_ID,),
            to_columns=(_ORDER_ID,),
            from_row_count=50,
            to_row_count=50,
        ),
    ),
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDiffPayloadTestCase(
            description="column drift is reported as added and removed names",
            expected_statuses=("changed", "unchanged"),
            expected_row_pairs=((1000, 1200), (50, 50)),
            expected_added_columns=(("region",), ()),
            expected_removed_columns=(("total",), ()),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_a_deployment_comparison_when_serializing_then_column_drift_is_named(
    test_case: DeploymentDiffPayloadTestCase,
) -> None:
    payload: dict[str, object] = deployment_diff_payload(result=_RESULT)

    relations: list[dict[str, object]] = cast(list[dict[str, object]], payload["relations"])
    assert tuple(str(item["status"]) for item in relations) == test_case.expected_statuses
    assert (
        tuple(
            (cast(int | None, item["fromRowCount"]), cast(int | None, item["toRowCount"]))
            for item in relations
        )
        == test_case.expected_row_pairs
    )
    assert (
        tuple(tuple(cast(list[str], item["addedColumns"])) for item in relations)
        == test_case.expected_added_columns
    )
    assert (
        tuple(tuple(cast(list[str], item["removedColumns"])) for item in relations)
        == test_case.expected_removed_columns
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
