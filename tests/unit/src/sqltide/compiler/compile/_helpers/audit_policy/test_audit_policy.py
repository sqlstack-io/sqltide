from pathlib import Path

import pytest

from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from tests.unit.src.sqltide.compiler.compile._helpers.audit_policy._test_types import (
    ValidateSqlAuditsErrorTestCase,
    ValidateSqlAuditsTestCase,
)
from tests.unit.src.sqltide.compiler.compile._helpers.audit_policy.helpers import (
    AUDIT_FILE_PATH,
    GENERIC_DEFINITION_FILE_PATH,
    ORDER_ITEMS_MODEL_FILE_PATH,
    validate_project_sql_audits,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ValidateSqlAuditsErrorTestCase(
            description="rejects unknown audit model refs",
            project_files=(
                (
                    AUDIT_FILE_PATH,
                    """
        AUDIT ();

        SELECT * FROM __ref("missing_model")
        """,
                ),
            ),
            expected_error_fragment="references unknown models: missing_model",
        ),
        ValidateSqlAuditsErrorTestCase(
            description="rejects unknown generic audit model refs",
            expected_error_fragment="references unknown models: missing_model",
            project_files=(
                (
                    GENERIC_DEFINITION_FILE_PATH,
                    """
        AUDIT ();

        SELECT *
        FROM __ref("@model") AS source_model
        INNER JOIN __ref("@other_model") AS other_model ON 1 = 1
        """,
                ),
                (
                    ORDER_ITEMS_MODEL_FILE_PATH,
                    """
        MODEL (
          order_by ["order_id"],
          audits [
            not_null (other_model missing_model, name "missing model generic audit"),
          ],
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(1 AS Nullable(Float64)) AS line_total
        FROM __ref("orders_clean")
        """,
                ),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sql_audits_when_validating_then_it_raises_clear_errors(
    test_case: ValidateSqlAuditsErrorTestCase,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        validate_project_sql_audits(
            tmp_path=tmp_path,
            project_files=test_case.project_files,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ValidateSqlAuditsTestCase(
            description="returns validated audits when all refs exist",
            project_files=(
                (
                    AUDIT_FILE_PATH,
                    """
        AUDIT (severity warning);

        SELECT * FROM __ref("order_items") WHERE line_total < 0
        """,
                ),
            ),
            expected_referenced_model_names=("order_items",),
            expected_severity="warning",
        ),
        ValidateSqlAuditsTestCase(
            description="returns validated rendered generic audits when all refs exist",
            project_files=(
                (
                    GENERIC_DEFINITION_FILE_PATH,
                    """
        AUDIT ();

        SELECT @column FROM __ref("@model") WHERE @column IS NULL
        """,
                ),
                (
                    ORDER_ITEMS_MODEL_FILE_PATH,
                    """
        MODEL (
          order_by ["order_id"],
          columns (
            order_id (audits [not_null (name "generic order id audit")]),
          ),
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(1 AS Nullable(Float64)) AS line_total
        FROM __ref("orders_clean")
        """,
                ),
            ),
            expected_referenced_model_names=("order_items",),
            expected_severity="error",
            expected_name="generic order id audit",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_sql_audits_when_validating_then_it_returns_loaded_audits(
    test_case: ValidateSqlAuditsTestCase,
    tmp_path: Path,
) -> None:
    loaded_audits: tuple[LoadedSqlAudit, ...] = validate_project_sql_audits(
        tmp_path=tmp_path,
        project_files=test_case.project_files,
    )

    assert loaded_audits[0].referenced_model_names == test_case.expected_referenced_model_names
    assert loaded_audits[0].severity == test_case.expected_severity
    assert loaded_audits[0].name == test_case.expected_name
