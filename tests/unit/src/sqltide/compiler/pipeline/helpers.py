import re
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

from sqltide._native import SqlModelAnalyzer, SqlReferenceRewriter, assemble_project
from sqltide.compiler.compile.models import CompiledProject, CompileProjectInputs


def assemble_project_with_completion(
    *,
    inputs: CompileProjectInputs,
    reference_rewriter: SqlReferenceRewriter,
    sql_analyzer: SqlModelAnalyzer,
    completion: Callable[[], object],
) -> CompiledProject:
    project: CompiledProject = assemble_project(
        inputs=inputs,
        reference_rewriter=reference_rewriter,
        sql_analyzer=sql_analyzer,
    )
    _ = completion()
    return project


def write_compilation_project(project_dir: Path) -> None:
    _write(
        project_dir / "sqltide_project.toml",
        """
        name = "compilation_project"
        default_target = "test"

        [defaults]
        pipeline_mode = \"virtual\"

        [targets.test]
        database = "analytics"
        """,
    )
    _write_pipeline(
        project_dir=project_dir,
        pipeline_name="zeta",
        source_name="zeta_source",
        model_name="zeta_model",
    )
    _write_pipeline(
        project_dir=project_dir,
        pipeline_name="alpha",
        source_name="alpha_source",
        model_name="alpha_model",
    )
    _write(
        project_dir / "tests" / "quality" / "alpha_test.sql",
        """
        TEST (name "shared test");

        WITH
          __source__alpha_source AS (
            SELECT CAST(1 AS UInt64) AS order_id
          ),
          __expected__alpha_model AS (
            SELECT CAST(@identity_sql('1') AS UInt64) AS order_id
          )
        SELECT 1
        """,
    )
    _write(
        project_dir / "audits" / "quality" / "alpha_audit.sql",
        """
        AUDIT (name "alpha quality");

        SELECT order_id FROM __ref("alpha_model") WHERE order_id = @identity_sql('0')
        """,
    )
    _write(
        project_dir / "macros" / "formatting.py",
        """
        def identity_sql(value: str) -> str:
            return value
        """,
    )


def write_macro_import_counter(project_dir: Path) -> Path:
    counter_path: Path = project_dir / "macro_imports.log"
    _write(
        project_dir / "macros" / "formatting.py",
        f"""
        from pathlib import Path

        counter_path = Path({str(counter_path)!r})
        with counter_path.open("a", encoding="utf-8") as counter_file:
            _ = counter_file.write("imported\\n")

        def identity_sql(value: str) -> str:
            return value
        """,
    )
    return counter_path


def write_duplicate_test(project_dir: Path) -> None:
    _write(
        project_dir / "tests" / "other" / "duplicate.sql",
        """
        TEST (name "shared test");

        WITH
          __source__alpha_source AS (
            SELECT CAST(1 AS UInt64) AS order_id
          ),
          __expected__alpha_model AS (
            SELECT CAST(1 AS UInt64) AS order_id
          )
        SELECT 1
        """,
    )


def write_source_model_name_collision(project_dir: Path) -> None:
    _write(
        project_dir / "pipelines" / "pl__alpha" / "alpha_source.sql",
        """
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("alpha_source")
        """,
    )


def write_shared_source_project(project_dir: Path) -> None:
    _write(
        project_dir / "sqltide_project.toml",
        """
        name = "shared_source_project"
        default_target = "test"

        [defaults]
        pipeline_mode = \"virtual\"

        [targets.test]
        database = "analytics"
        """,
    )
    _write(
        project_dir / "sources" / "orders.yml",
        """
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
        """,
    )
    _write_shared_source_pipeline(
        project_dir=project_dir,
        pipeline_name="alpha",
        model_name="alpha_orders",
    )
    _write_shared_source_pipeline(
        project_dir=project_dir,
        pipeline_name="zeta",
        model_name="zeta_orders",
    )


def write_managed_source_ttl_project(
    *, project_dir: Path, project_default_ttl: str, source_ttl_declaration: str
) -> None:
    _write(
        project_dir / "sqltide_project.toml",
        f"""
        name = "managed_source_ttl_project"
        default_target = "test"

        [defaults]
        managed_source_ttl = "{project_default_ttl}"

        [targets.test]
        database = "analytics"
        """,
    )
    _write(
        project_dir / "sources" / "orders.yml",
        f"""
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            {source_ttl_declaration}
            replay_boundary: {{mode: offsets}}
        """,
    )
    _write(
        project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql",
        """
        MODEL (order_by ["order_id"]);

        SELECT CAST(kafka_value AS UInt64) AS order_id FROM __source("orders")
        """,
    )


def write_managed_source_retention_project(
    *, project_dir: Path, source_retention_declaration: str
) -> None:
    _write(
        project_dir / "sqltide_project.toml",
        """
        name = "managed_source_retention_project"
        default_target = "test"

        [defaults.sources.kafka.retention]
        duration = "7d"
        timestamp = "broker"
        fallback = "landed"
        cap_at = "landed"

        [targets.test]
        database = "analytics"
        """,
    )
    _write(
        project_dir / "sources" / "orders.yml",
        f"""
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            {source_retention_declaration}
            replay_boundary: {{mode: offsets}}
        """,
    )
    _write(
        project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql",
        """
        MODEL (order_by ["order_id"]);

        SELECT CAST(kafka_value AS UInt64) AS order_id FROM __source("orders")
        """,
    )


def write_policy_validation_project(
    *,
    project_dir: Path,
    project_contents: str,
    local_contents: str,
    pipeline_config_contents: str,
    model_contents: str,
) -> None:
    _write(project_dir / "sqltide_project.toml", project_contents)
    _write(project_dir / "sqltide_local.toml", local_contents)
    _write(
        project_dir / "sources" / "orders.yml",
        """
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
        """,
    )
    _write(
        project_dir / "pipelines" / "pl__orders" / "pipeline.toml",
        pipeline_config_contents,
    )
    _write(project_dir / "pipelines" / "pl__orders" / "orders_enriched.sql", model_contents)


def has_compilation_service_import(file_path: Path) -> bool:
    return (
        re.search(
            r"from sqltide\._native import (?:\([^)]*|[^\n]*)\banalyze_project\b",
            file_path.read_text(encoding="utf-8"),
        )
        is not None
    )


def _write_pipeline(
    *, project_dir: Path, pipeline_name: str, source_name: str, model_name: str
) -> None:
    pipeline_dir: Path = project_dir / "pipelines" / f"pl__{pipeline_name}"
    _write(
        project_dir / "sources" / f"{source_name}.yml",
        f"""
        sources:
          - name: {source_name}
            kind: kafka
            broker_list: kafka:9092
            topic: source.{source_name}
            replay_boundary:
              mode: offsets
        """,
    )
    _write(
        pipeline_dir / f"{model_name}.sql",
        f"""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
          settings (index_granularity "8192"),
        );

        SELECT @identity_sql('CAST(order_id AS UInt64)') AS order_id FROM __ref("{source_name}")
        """,
    )


def _write_shared_source_pipeline(
    *, project_dir: Path, pipeline_name: str, model_name: str
) -> None:
    pipeline_dir: Path = project_dir / "pipelines" / f"pl__{pipeline_name}"
    _write(
        pipeline_dir / f"{model_name}.sql",
        """
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
        );

        SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
        """,
    )


def _write(file_path: Path, contents: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(dedent(contents).strip() + "\n", encoding="utf-8")
