from pathlib import Path

from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from tests.unit.src.sqltide.compiler.audit_discovery.helpers import write_sql_audit_file
from tests.unit.src.sqltide.compiler.compile.helpers import compile_logical_project
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
    write_project_configuration_and_source,
)

AUDIT_FILE_PATH: str = "audits/order_events/audit.sql"
GENERIC_DEFINITION_FILE_PATH: str = "audits/generic/not_null.sql"
ORDER_ITEMS_MODEL_FILE_PATH: str = "pipelines/order_events/order_items.sql"


def validate_project_sql_audits(
    *,
    tmp_path: Path,
    project_files: tuple[tuple[str, str], ...],
) -> tuple[LoadedSqlAudit, ...]:
    write_project_configuration_and_source(project_dir=tmp_path)
    write_pipeline_file(
        tmp_path / "pipelines" / "order_events" / "orders_clean.sql",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT CAST(order_id AS String) AS order_id
        FROM __ref("orders")
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / "order_events" / "order_items.sql",
        """
        MODEL (
          order_by ["order_id"]
        );

        SELECT
          CAST(order_id AS String) AS order_id,
          CAST(1 AS Nullable(Float64)) AS line_total
        FROM __ref("orders_clean")
        """,
    )
    relative_path: str
    file_contents: str
    for relative_path, file_contents in project_files:
        write_sql_audit_file(tmp_path / relative_path, file_contents)
    return compile_logical_project(tmp_path).audits
