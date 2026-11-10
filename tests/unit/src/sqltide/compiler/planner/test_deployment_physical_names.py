import pytest

from sqltide._native import (
    deployment_id_from_physical_name,
    is_deployment_physical_name,
    logical_name_from_physical_name,
)
from tests.unit.src.sqltide.compiler.planner._test_types import (
    DeploymentPhysicalNameParsingTestCase,
    DeploymentPhysicalNameRecognitionTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentPhysicalNameRecognitionTestCase(
            description="recognizes a managed name with an opaque deployment suffix",
            physical_name="tbl__orders_enriched__20260410T130000Z_ab12cd",
            expected_is_deployment_name=True,
        ),
        DeploymentPhysicalNameRecognitionTestCase(
            description="recognizes a custom model name with a deployment suffix",
            physical_name="orders_enriched__20260410T130000Z_ab12cd",
            expected_is_deployment_name=True,
        ),
        DeploymentPhysicalNameRecognitionTestCase(
            description="rejects a managed logical name without a deployment suffix",
            physical_name="tbl__orders_enriched",
            expected_is_deployment_name=False,
        ),
        DeploymentPhysicalNameRecognitionTestCase(
            description="rejects a managed name with an empty deployment suffix",
            physical_name="tbl__orders_enriched__",
            expected_is_deployment_name=False,
        ),
        DeploymentPhysicalNameRecognitionTestCase(
            description="rejects an arbitrary non-deployment suffix",
            physical_name="orders_enriched__draft",
            expected_is_deployment_name=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_physical_name_when_recognizing_deployment_name_then_returns_current_classification(
    test_case: DeploymentPhysicalNameRecognitionTestCase,
) -> None:
    result: bool = is_deployment_physical_name(test_case.physical_name)

    assert result is test_case.expected_is_deployment_name


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentPhysicalNameParsingTestCase(
            description="splits on the final separator so managed logical separators are retained",
            physical_name="tbl__orders__daily__20260410T130000Z_ab12cd",
            expected_logical_name="tbl__orders__daily",
            expected_deployment_id="20260410T130000Z_ab12cd",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_recognized_deployment_name_when_parsing_then_returns_logical_name_and_deployment_id(
    test_case: DeploymentPhysicalNameParsingTestCase,
) -> None:
    logical_name: str = logical_name_from_physical_name(test_case.physical_name)
    deployment_id: str = deployment_id_from_physical_name(test_case.physical_name)

    assert logical_name == test_case.expected_logical_name
    assert deployment_id == test_case.expected_deployment_id
