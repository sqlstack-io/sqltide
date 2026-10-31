from pathlib import Path

import pytest

from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.compile._helpers.content import static_test_sql
from sqltide.cli.compile._helpers.paths import (
    audit_path,
    runtime_test_path,
    source_resource_path,
    static_test_path,
)
from sqltide.cli.compile._helpers.publication import _stage_artifacts
from sqltide.cli.compile._helpers.static_artifacts import _validate_unique_paths
from sqltide.cli.compile.exceptions import CompileArtifactError
from sqltide.cli.compile.models import StaticArtifactFile, StaticCompileArtifacts
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.sql_analysis.main._validate_query import validate_query
from sqltide.compiler.testing._helpers.comparison import render_comparison_query
from sqltide.compiler.testing.models import SqlTestCase, SqlTestChainStep
from tests.unit.src.sqltide.cli.compile._test_types import (
    AuditArtifactPathTestCase,
    DuplicateArtifactPathTestCase,
    MultiTargetTestSqlTestCase,
    SqlTestArtifactPathTestCase,
    UnsafeArtifactPathTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        SqlTestArtifactPathTestCase(
            description="uses one target folder for a single-target SQL test",
            test_name="one target",
            target_names=("orders",),
            expected_static_path="compiled/tests/orders/one target.sql",
            expected_runtime_path="run/tests/orders/one target.sql",
        ),
        SqlTestArtifactPathTestCase(
            description="uses sorted deduplicated chain targets for a multi-target SQL test",
            test_name="chain test",
            target_names=("zeta", "alpha", "zeta", "beta"),
            expected_static_path="compiled/tests/_chain_/alpha__beta__zeta/chain test.sql",
            expected_runtime_path="run/tests/_chain_/alpha__beta__zeta/chain test.sql",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_sql_test_when_resolving_artifact_path_then_uses_target_contract(
    test_case: SqlTestArtifactPathTestCase,
) -> None:
    test_case_model: SqlTestCase = SqlTestCase(
        file_path=Path("tests/example.sql"),
        query="SELECT 1",
        name=test_case.test_name,
        target_cases=tuple(
            SqlTestChainStep(
                target_model_name=target_name,
                expected_column_names=("id",),
                ctes=(),
                actual_query="SELECT 1 AS id",
                expected_query="SELECT 1 AS id",
            )
            for target_name in test_case.target_names
        ),
    )

    assert static_test_path(test_case=test_case_model) == (test_case.expected_static_path)
    assert runtime_test_path(test_case=test_case_model).as_posix() == (
        test_case.expected_runtime_path
    )


@pytest.mark.parametrize(
    "test_case",
    [
        AuditArtifactPathTestCase(
            description="mirrors audit subfolders and appends a stable named block",
            project_dir="project",
            audit_file_path="project/audits/orders/checks.sql",
            audit_name="not null",
            expected_path="compiled/audits/orders/checks__not null.sql",
        ),
        AuditArtifactPathTestCase(
            description="anchors audit mirroring to a project nested below another audits folder",
            project_dir="workspace/audits/container/project",
            audit_file_path=("workspace/audits/container/project/audits/orders/checks.sql"),
            audit_name="not null",
            expected_path="compiled/audits/orders/checks__not null.sql",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_named_audit_when_resolving_artifact_path_then_mirrors_authored_subtree(
    test_case: AuditArtifactPathTestCase,
) -> None:
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path(test_case.audit_file_path),
        query="SELECT 1",
        referenced_model_names=("orders",),
        name=test_case.audit_name,
    )

    assert audit_path(audit=audit, project_dir=Path(test_case.project_dir)) == (
        test_case.expected_path
    )


@pytest.mark.parametrize(
    "test_case",
    [
        UnsafeArtifactPathTestCase(
            description="rejects parent traversal in a source identity",
            unsafe_name="../escaped",
            expected_error_fragment="Unsafe source artifact path segment",
        ),
        UnsafeArtifactPathTestCase(
            description="rejects an absolute source identity",
            unsafe_name="/tmp/escaped",
            expected_error_fragment="Unsafe source artifact path segment",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_logical_name_when_resolving_path_then_rejects_escape(
    test_case: UnsafeArtifactPathTestCase,
) -> None:
    with pytest.raises(CompileArtifactError, match=test_case.expected_error_fragment):
        source_resource_path(source_name=test_case.unsafe_name, resource_name="kafka__orders")


@pytest.mark.parametrize(
    "test_case",
    [
        UnsafeArtifactPathTestCase(
            description="rejects traversal in a SQL test name",
            unsafe_name="../escaped",
            expected_error_fragment="Unsafe SQL test artifact path segment",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_test_name_when_resolving_path_then_rejects_escape(
    test_case: UnsafeArtifactPathTestCase,
) -> None:
    compiled_test: SqlTestCase = SqlTestCase(
        file_path=Path("tests/check.sql"),
        query="SELECT 1",
        name=test_case.unsafe_name,
        target_cases=(
            SqlTestChainStep(
                target_model_name="orders",
                expected_column_names=("id",),
                ctes=(),
                actual_query="SELECT 1 AS id",
                expected_query="SELECT 1 AS id",
            ),
        ),
    )

    with pytest.raises(CompileArtifactError, match=test_case.expected_error_fragment):
        static_test_path(test_case=compiled_test)


@pytest.mark.parametrize(
    "test_case",
    [
        UnsafeArtifactPathTestCase(
            description="rejects traversal in an audit name",
            unsafe_name="../escaped",
            expected_error_fragment="Unsafe audit artifact path segment",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_audit_name_when_resolving_path_then_rejects_escape(
    test_case: UnsafeArtifactPathTestCase,
) -> None:
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("project/audits/check.sql"),
        query="SELECT 1",
        referenced_model_names=("orders",),
        name=test_case.unsafe_name,
    )

    with pytest.raises(CompileArtifactError, match=test_case.expected_error_fragment):
        audit_path(audit=audit, project_dir=Path("project"))


@pytest.mark.parametrize(
    "test_case",
    [
        UnsafeArtifactPathTestCase(
            description="rejects a crafted artifact path that escapes staging",
            unsafe_name="../escaped.sql",
            expected_error_fragment="escapes the target root",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_crafted_relative_path_when_staging_then_enforces_root_containment(
    test_case: UnsafeArtifactPathTestCase,
    tmp_path: Path,
) -> None:
    artifacts: StaticCompileArtifacts = StaticCompileArtifacts(
        compiled_files=(
            StaticArtifactFile(relative_path=test_case.unsafe_name, contents="SELECT 1\n"),
        ),
        manifest_json="{}\n",
        dag_json="{}\n",
    )

    with pytest.raises(CompileArtifactError, match=test_case.expected_error_fragment):
        _stage_artifacts(artifacts=artifacts, staging_root=tmp_path / "staging")
    assert not (tmp_path / "escaped.sql").exists()


@pytest.mark.parametrize(
    "test_case",
    [
        DuplicateArtifactPathTestCase(
            description="rejects two static artifacts that resolve to one path",
            duplicate_path="compiled/audits/checks__foo.sql",
            expected_error_fragment="Multiple compile artifacts resolve to",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_duplicate_static_paths_when_validating_then_rejects_collision(
    test_case: DuplicateArtifactPathTestCase,
) -> None:
    duplicate_path: str = test_case.duplicate_path
    files: tuple[StaticArtifactFile, ...] = (
        StaticArtifactFile(relative_path=duplicate_path, contents="SELECT 1\n"),
        StaticArtifactFile(relative_path=duplicate_path, contents="SELECT 2\n"),
    )

    with pytest.raises(CompileArtifactError, match=test_case.expected_error_fragment):
        _validate_unique_paths(files=files)


@pytest.mark.parametrize(
    "test_case",
    [
        MultiTargetTestSqlTestCase(
            description="assembles multiple target comparisons into one executable statement",
            target_names=("orders", "payments"),
            expected_fragments=(
                "0 AS _case_index",
                "1 AS _case_index",
                "'missing' AS _diff_type",
                "'unexpected' AS _diff_type",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multi_target_test_when_assembling_static_sql_then_emits_one_statement(
    test_case: MultiTargetTestSqlTestCase,
) -> None:
    target_cases: tuple[SqlTestChainStep, ...] = tuple(
        SqlTestChainStep(
            target_model_name=target_name,
            expected_column_names=("id",),
            ctes=(),
            actual_query="SELECT 1 AS id",
            expected_query="SELECT 1 AS id",
        )
        for target_name in test_case.target_names
    )
    compiled_test: SqlTestCase = SqlTestCase(
        file_path=Path("tests/chain.sql"),
        query=render_comparison_query(
            comparison_renderer=ClickHouseAdapter().render_set_difference_comparison,
            target_cases=target_cases,
            assertion_cases=(),
        ),
        name="chain",
        target_cases=target_cases,
    )

    sql: str = static_test_sql(test_case=compiled_test)

    validate_query(sql=sql, dialect="clickhouse")
    assert tuple(fragment in sql for fragment in test_case.expected_fragments) == (
        True,
        True,
        True,
        True,
    )
