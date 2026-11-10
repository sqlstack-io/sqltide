from pathlib import Path

import pytest

from sqltide._native import write_test_runtime_target
from sqltide.compiler.testing.models import SqlTestCase
from sqltide.executor.testing.models import SqlTestExecutionResult
from tests.unit.src.sqltide.cli.test._helpers._test_types import (
    RuntimeTestArtifactTestCase,
)
from tests.unit.src.sqltide.cli.test._helpers.helpers import (
    build_runtime_test_cases,
    build_runtime_test_results,
    seed_existing_target_tree,
)


@pytest.mark.parametrize(
    "test_case",
    [
        RuntimeTestArtifactTestCase(
            description="writes executed bytes for a single target test",
            target_model_names=("order_items",),
            test_name="line total",
            executed_sql="SELECT 0 AS _case_index -- executed\n",
            expected_relative_path="run/tests/order_items/line total.sql",
        ),
        RuntimeTestArtifactTestCase(
            description="writes executed bytes into the chain folder for many targets",
            target_model_names=("zeta", "alpha"),
            test_name="chain test",
            executed_sql="SELECT 1 AS _case_index\n",
            expected_relative_path="run/tests/_chain_/alpha__zeta/chain test.sql",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_executed_sql_tests_when_writing_runtime_target_then_bytes_match_adapter_input(
    test_case: RuntimeTestArtifactTestCase,
    tmp_path: Path,
) -> None:
    target_dir: Path = tmp_path / "target"
    test_cases: tuple[SqlTestCase, ...] = build_runtime_test_cases(
        target_model_names=test_case.target_model_names,
        test_name=test_case.test_name,
        executed_sql=test_case.executed_sql,
    )
    results: tuple[SqlTestExecutionResult, ...] = build_runtime_test_results(
        test_cases=test_cases,
        executed_sql=test_case.executed_sql,
    )

    write_test_runtime_target(target_dir=target_dir, test_cases=test_cases, results=results)

    written_path: Path = target_dir / test_case.expected_relative_path
    assert written_path.read_bytes() == test_case.executed_sql.encode("utf-8")


@pytest.mark.parametrize(
    "test_case",
    [
        RuntimeTestArtifactTestCase(
            description="replaces only the runtime tests subtree",
            target_model_names=("order_items",),
            test_name="line total",
            executed_sql="SELECT 2 AS _case_index\n",
            expected_relative_path="run/tests/order_items/line total.sql",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_target_tree_when_writing_runtime_target_then_it_owns_only_run_tests(
    test_case: RuntimeTestArtifactTestCase,
    tmp_path: Path,
) -> None:
    target_dir: Path = tmp_path / "target"
    seed_existing_target_tree(target_dir=target_dir)
    test_cases: tuple[SqlTestCase, ...] = build_runtime_test_cases(
        target_model_names=test_case.target_model_names,
        test_name=test_case.test_name,
        executed_sql=test_case.executed_sql,
    )
    results: tuple[SqlTestExecutionResult, ...] = build_runtime_test_results(
        test_cases=test_cases,
        executed_sql=test_case.executed_sql,
    )

    write_test_runtime_target(target_dir=target_dir, test_cases=test_cases, results=results)

    assert (target_dir / "compiled" / "models" / "orders" / "stale.sql").read_text(
        encoding="utf-8"
    ) == "SELECT 'compiled'\n"
    assert (target_dir / "manifest.json").read_text(encoding="utf-8") == "{}\n"
    assert (target_dir / "sqltide_dag.json").read_text(encoding="utf-8") == "{}\n"
    assert (target_dir / "run" / "other" / "keep.sql").read_text(encoding="utf-8") == "SELECT 3\n"
    assert not (target_dir / "run" / "tests" / "order_items" / "stale.sql").exists()
    assert (target_dir / test_case.expected_relative_path).read_bytes() == (
        test_case.executed_sql.encode("utf-8")
    )
    assert tuple(sorted(path.name for path in target_dir.iterdir())) == (
        "compiled",
        "manifest.json",
        "run",
        "sqltide_dag.json",
    )
