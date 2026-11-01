from pathlib import Path

from sqltide.compiler.audit_discovery.main._build_model_audit_instances import (
    build_model_audit_instances,
)
from sqltide.compiler.audit_discovery.main._discover_sql_audits import discover_sql_audits
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile._helpers.audit_policy import resolve_audit_policies
from sqltide.compiler.compile.main._compile_pipeline import compile_pipeline
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.discovery.main._discover_pipelines import discover_pipelines
from sqltide.compiler.discovery.models import AuditDefaults, TransformStep, ViewStep
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from tests.unit.src.sqltide.compiler.audit_discovery.helpers import write_sql_audit_file
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
    sql_analyzer: SqlModelAnalyzer = SqlModelAnalyzer(dialect="clickhouse")
    loaded_pipelines: tuple = tuple(discover_pipelines(tmp_path / "pipelines"))
    compiled_pipelines: tuple[CompiledPipeline, ...] = tuple(
        compile_pipeline(loaded_pipeline=loaded_pipeline, sql_analyzer=sql_analyzer)
        for loaded_pipeline in loaded_pipelines
    )
    model_steps: list[TransformStep | ViewStep] = []
    for loaded_pipeline in loaded_pipelines:
        model_steps.extend(loaded_pipeline.pipeline.transforms)
    loaded_audits: tuple[LoadedSqlAudit, ...] = tuple(
        discover_sql_audits(
            root=tmp_path / "audits",
            generic_audit_instances=build_model_audit_instances(models=tuple(model_steps)),
        )
    )
    return resolve_audit_policies(
        audits=loaded_audits,
        compiled_pipelines=compiled_pipelines,
        project_defaults=AuditDefaults(),
    )
