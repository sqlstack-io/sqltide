from pathlib import Path

import sqltide._native as _native
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase, SqlTestChainStep
from sqltide.executor.testing.models import SqlTestExecutionResult, SqlTestTargetExecutionResult
from tests.unit.src.sqltide.cli.plan.main.helpers import (
    compile_selector_project_pipelines,
)
from tests.unit.src.sqltide.compiler.test_discovery.helpers import (
    write_sql_test_file,
)


def build_selector_project_loaded_tests(tmp_path: Path) -> tuple[LoadedSqlTest, ...]:
    tests_root: Path = tmp_path / "tests"
    write_sql_test_file(
        tests_root / "orders" / "test_orders_clean.sql",
        """
        TEST ();

        WITH
        __source__orders AS (
          SELECT '{"order_id":"ord_001"}' AS kafka_value,
                 now64(3) AS _replay_landed_at
        ),
        __expected__orders_clean AS (
          SELECT 'ord_001' AS order_id, now64(3) AS _replay_landed_at
        )
        SELECT 1
        """,
    )
    write_sql_test_file(
        tests_root / "orders" / "test_orders_enriched.sql",
        """
        TEST ();

        WITH
        __ref__orders_clean AS (
          SELECT 'ord_001' AS order_id, now64(3) AS _replay_landed_at
        ),
        __expected__orders_enriched AS (
          SELECT 'ord_001' AS order_id, now64(3) AS _replay_landed_at
        )
        SELECT 1
        """,
    )
    write_sql_test_file(
        tests_root / "payments" / "test_payments_enriched.sql",
        """
        TEST ();

        WITH
        __source__payments AS (
          SELECT '{"payment_id":"pay_001"}' AS kafka_value,
                 now64(3) AS _replay_landed_at
        ),
        __expected__payments_enriched AS (
          SELECT 'pay_001' AS payment_id, now64(3) AS _replay_landed_at
        )
        SELECT 1
        """,
    )
    return tuple(_native.discover_sql_tests(root=tests_root))


def build_selector_project_compiled_pipelines() -> tuple[CompiledPipeline, ...]:
    return compile_selector_project_pipelines()


def build_render_sql_test_results(description: str) -> tuple[SqlTestExecutionResult, ...]:
    results_by_description: dict[str, tuple[SqlTestExecutionResult, ...]] = {
        "renders side by side diff for keyed row changes": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_line_total.sql"),
                passed=False,
                target_results=(
                    SqlTestTargetExecutionResult(
                        target_model_name="order_items",
                        passed=False,
                        column_names=("order_id", "line_total"),
                        missing_rows=(("ord_001", 25.0),),
                        unexpected_rows=(("ord_001", 20.0),),
                    ),
                ),
                executed_sql="SELECT 1",
                name="line total computes correctly",
            ),
        ),
        "renders aligned missing and unexpected tables when rows do not share a key": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_regions.sql"),
                passed=False,
                target_results=(
                    SqlTestTargetExecutionResult(
                        target_model_name="order_items",
                        passed=False,
                        column_names=("order_id", "line_total", "region"),
                        missing_rows=(("ord_001", 25.0, "us-east"),),
                        unexpected_rows=(("ord_004", 99.0, "ap-south"),),
                    ),
                ),
                executed_sql="SELECT 1",
            ),
        ),
        "renders blank lines between failed multi target sections": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_multi.sql"),
                passed=False,
                target_results=(
                    SqlTestTargetExecutionResult(
                        target_model_name="order_items",
                        passed=False,
                        column_names=("order_id", "line_total"),
                        missing_rows=(("ord_001", 25.0),),
                        unexpected_rows=(("ord_001", 20.0),),
                    ),
                    SqlTestTargetExecutionResult(
                        target_model_name="daily_revenue",
                        passed=False,
                        column_names=("order_id", "line_total"),
                        missing_rows=(("ord_001", 30.0),),
                        unexpected_rows=(("ord_001", 20.0),),
                    ),
                ),
                executed_sql="SELECT 1",
            ),
        ),
        "truncates long sections when not verbose": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_many_rows.sql"),
                passed=False,
                target_results=(
                    SqlTestTargetExecutionResult(
                        target_model_name="order_items",
                        passed=False,
                        column_names=("order_id", "line_total"),
                        missing_rows=(),
                        unexpected_rows=tuple(
                            (f"ord_{index:03d}", float(index)) for index in range(1, 13)
                        ),
                    ),
                ),
                executed_sql="SELECT 1",
            ),
        ),
        "renders all rows when verbose": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_many_rows.sql"),
                passed=False,
                target_results=(
                    SqlTestTargetExecutionResult(
                        target_model_name="order_items",
                        passed=False,
                        column_names=("order_id", "line_total"),
                        missing_rows=(),
                        unexpected_rows=tuple(
                            (f"ord_{index:03d}", float(index)) for index in range(1, 13)
                        ),
                    ),
                ),
                executed_sql="SELECT 1",
            ),
        ),
        "renders a warehouse execution error without target diffs": (
            SqlTestExecutionResult(
                file_path=Path("/project/tests/order_events/test_broken.sql"),
                passed=False,
                target_results=(),
                executed_sql="SELECT broken",
                error_message="warehouse rejected test SQL",
            ),
        ),
    }
    return results_by_description[description]


def build_runtime_test_cases(
    *,
    target_model_names: tuple[str, ...],
    test_name: str,
    executed_sql: str,
) -> tuple[SqlTestCase, ...]:
    return (
        SqlTestCase(
            file_path=Path("/project/tests/order_events/test_line_total.sql"),
            query=executed_sql,
            target_cases=tuple(
                SqlTestChainStep(
                    target_model_name=target_model_name,
                    expected_column_names=("order_id",),
                    ctes=(),
                    actual_query="SELECT order_id FROM __model__order_items",
                    expected_query="SELECT 'ord_001' AS order_id",
                )
                for target_model_name in target_model_names
            ),
            name=test_name,
        ),
    )


def build_runtime_test_results(
    *, test_cases: tuple[SqlTestCase, ...], executed_sql: str
) -> tuple[SqlTestExecutionResult, ...]:
    return tuple(
        SqlTestExecutionResult(
            file_path=test_case.file_path,
            test_index=test_case.test_index,
            passed=True,
            target_results=(),
            executed_sql=executed_sql,
            name=test_case.name,
        )
        for test_case in test_cases
    )


def seed_existing_target_tree(*, target_dir: Path) -> None:
    compiled_path: Path = target_dir / "compiled" / "models" / "orders" / "stale.sql"
    compiled_path.parent.mkdir(parents=True, exist_ok=True)
    compiled_path.write_text("SELECT 'compiled'\n", encoding="utf-8")
    (target_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    (target_dir / "sqltide_dag.json").write_text("{}\n", encoding="utf-8")
    stale_runtime_path: Path = target_dir / "run" / "tests" / "order_items" / "stale.sql"
    stale_runtime_path.parent.mkdir(parents=True, exist_ok=True)
    stale_runtime_path.write_text("SELECT 'stale'\n", encoding="utf-8")
    other_runtime_path: Path = target_dir / "run" / "other" / "keep.sql"
    other_runtime_path.parent.mkdir(parents=True, exist_ok=True)
    other_runtime_path.write_text("SELECT 3\n", encoding="utf-8")


MACRO_SQL_TEST_CONTENTS: str = """
TEST (mode macro, name "orphan macro check");

WITH
__macro_actual__ AS (
  SELECT 42 AS doubled
),
__macro_expected__ AS (
  SELECT 42 AS doubled
)
SELECT 1
"""


def build_selector_project_loaded_tests_with_macro(tmp_path: Path) -> tuple[LoadedSqlTest, ...]:
    tests_root: Path = tmp_path / "tests"
    loaded_tests: tuple[LoadedSqlTest, ...] = build_selector_project_loaded_tests(tmp_path)
    write_sql_test_file(tests_root / "macros" / "test_orphan_macro.sql", MACRO_SQL_TEST_CONTENTS)
    del loaded_tests
    return tuple(_native.discover_sql_tests(root=tests_root))
