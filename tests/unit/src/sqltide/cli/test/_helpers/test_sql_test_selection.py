from itertools import chain
from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.cli.entry.exceptions import CliUserError
from sqltide.cli.test._helpers.selection import select_loaded_sql_tests
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from tests.unit.src.sqltide.cli.test._helpers._test_types import (
    MacroSqlTestSelectionTestCase,
    SelectLoadedSqlTestsErrorTestCase,
    SelectLoadedSqlTestsTestCase,
)
from tests.unit.src.sqltide.cli.test._helpers.helpers import (
    build_selector_project_compiled_pipelines,
    build_selector_project_loaded_tests,
    build_selector_project_loaded_tests_with_macro,
)


@pytest.mark.parametrize(
    "test_case",
    [
        SelectLoadedSqlTestsTestCase(
            description="bare model selector includes tests for that target only",
            selectors=("orders_clean",),
            paths=(),
            expected_target_model_names=("orders_clean",),
        ),
        SelectLoadedSqlTestsTestCase(
            description="pipeline selector includes tests for all models in that pipeline",
            selectors=("pipeline:pl__orders",),
            paths=(),
            expected_target_model_names=("orders_clean", "orders_enriched"),
        ),
        SelectLoadedSqlTestsTestCase(
            description="downstream plus selector includes downstream target tests",
            selectors=("orders_clean+",),
            paths=(),
            expected_target_model_names=("orders_clean", "orders_enriched"),
        ),
        SelectLoadedSqlTestsTestCase(
            description="upstream plus selector includes upstream target tests",
            selectors=("+orders_enriched",),
            paths=(),
            expected_target_model_names=("orders_clean", "orders_enriched"),
        ),
        SelectLoadedSqlTestsTestCase(
            description="multiple selectors union correctly",
            selectors=("orders_clean", "pipeline:pl__payments"),
            paths=(),
            expected_target_model_names=("orders_clean", "payments_enriched"),
        ),
        SelectLoadedSqlTestsTestCase(
            description="graph and plain selectors union correctly",
            selectors=("orders_clean+", "payments_enriched"),
            paths=(),
            expected_target_model_names=(
                "orders_clean",
                "orders_enriched",
                "payments_enriched",
            ),
        ),
        SelectLoadedSqlTestsTestCase(
            description="path selections union with selector matches",
            selectors=("payments_enriched",),
            paths=("tests/orders/test_orders_enriched.sql",),
            expected_target_model_names=("orders_enriched", "payments_enriched"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_test_selectors_when_selecting_then_it_returns_expected_targets(
    test_case: SelectLoadedSqlTestsTestCase,
    tmp_path: Path,
) -> None:
    loaded_tests: tuple[LoadedSqlTest, ...] = build_selector_project_loaded_tests(tmp_path)

    selected_tests: tuple[LoadedSqlTest, ...] = select_loaded_sql_tests(
        loaded_tests=loaded_tests,
        compiled_pipelines=build_selector_project_compiled_pipelines(),
        selectors=test_case.selectors,
        paths=tuple(Path(path) for path in test_case.paths),
        project_dir=tmp_path,
    )

    selected_target_model_names: tuple[str, ...] = tuple(
        sorted(
            set(
                chain.from_iterable(
                    _native.sql_test_target_names(loaded_test=test) for test in selected_tests
                )
            )
        )
    )

    assert selected_target_model_names == tuple(sorted(test_case.expected_target_model_names))


@pytest.mark.parametrize(
    "test_case",
    [
        SelectLoadedSqlTestsErrorTestCase(
            description="invalid selector syntax fails clearly",
            selectors=("++orders_clean",),
            paths=(),
            expected_error_fragment="Unsupported test selector syntax",
        ),
        SelectLoadedSqlTestsErrorTestCase(
            description="unknown selector namespace fails clearly",
            selectors=("tag:finance",),
            paths=(),
            expected_error_fragment="Unsupported test selector namespace 'tag'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_test_selectors_when_selecting_then_it_raises_clear_errors(
    test_case: SelectLoadedSqlTestsErrorTestCase,
    tmp_path: Path,
) -> None:
    loaded_tests: tuple[LoadedSqlTest, ...] = build_selector_project_loaded_tests(tmp_path)

    with pytest.raises(CliUserError, match=test_case.expected_error_fragment):
        select_loaded_sql_tests(
            loaded_tests=loaded_tests,
            compiled_pipelines=build_selector_project_compiled_pipelines(),
            selectors=test_case.selectors,
            paths=tuple(Path(path) for path in test_case.paths),
            project_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        MacroSqlTestSelectionTestCase(
            description="no selector runs every test including an orphan macro test",
            selectors=(),
            paths=(),
            expected_selected_file_names=(
                "test_orphan_macro.sql",
                "test_orders_clean.sql",
                "test_orders_enriched.sql",
                "test_payments_enriched.sql",
            ),
        ),
        MacroSqlTestSelectionTestCase(
            description="an explicit path selects the orphan macro test",
            selectors=(),
            paths=("tests/macros/test_orphan_macro.sql",),
            expected_selected_file_names=("test_orphan_macro.sql",),
        ),
        MacroSqlTestSelectionTestCase(
            description="a model name selector never matches a macro test",
            selectors=("orders_clean",),
            paths=(),
            expected_selected_file_names=("test_orders_clean.sql",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_orphan_macro_test_when_selecting_then_only_default_and_path_selection_match(
    test_case: MacroSqlTestSelectionTestCase,
    tmp_path: Path,
) -> None:
    loaded_tests: tuple[LoadedSqlTest, ...] = build_selector_project_loaded_tests_with_macro(
        tmp_path
    )

    selected_tests: tuple[LoadedSqlTest, ...] = select_loaded_sql_tests(
        loaded_tests=loaded_tests,
        compiled_pipelines=build_selector_project_compiled_pipelines(),
        selectors=test_case.selectors,
        paths=tuple(Path(path) for path in test_case.paths),
        project_dir=tmp_path,
    )

    assert tuple(sorted({test.file_path.name for test in selected_tests})) == tuple(
        sorted(test_case.expected_selected_file_names)
    )
