"""CLI command for executing SQL-native model tests."""

from __future__ import annotations

from pathlib import Path

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.models import AdapterInvocationRecord, AdapterNodeResultRecord
from sqltide.cli.test._helpers.rendering import render_sql_test_results
from sqltide.cli.test._helpers.runtime_artifacts import write_test_runtime_target
from sqltide.cli.test._helpers.selection import select_loaded_sql_tests
from sqltide.cli.test.constants import DEFAULT_TARGET_DIRECTORY_NAME
from sqltide.compiler.compile.models import CompiledPipeline, CompilerAdapterProfile
from sqltide.compiler.discovery.models import LoadedProject
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase
from sqltide.executor.observability.main.build_invocation_record import (
    build_invocation_record,
)
from sqltide.executor.observability.main.build_node_result_record import (
    build_node_result_record,
)
from sqltide.executor.observability.main.persist_terminal_observations import (
    persist_terminal_observations,
)
from sqltide.executor.observability.main.start_invocation import start_invocation
from sqltide.executor.observability.models import QualityResultContext, TerminalInvocation
from sqltide.executor.observability.types import QualityResultTrigger
from sqltide.executor.testing.main.execute_sql_tests import execute_sql_tests
from sqltide.executor.testing.models import (
    SqlTestExecutionResult,
    SqlTestTargetExecutionResult,
)


def run_test(
    *,
    pipelines_root: Path,
    project_dir: Path | None,
    selectors: tuple[str, ...],
    paths: tuple[Path, ...],
    verbose: bool,
    client: AdapterConnection,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    target_dir: Path | None = None,
    database: str = "",
) -> int:
    """Discover, assemble, and execute SQL-native tests for a project."""

    started: tuple[str, str, int] = start_invocation()
    resolved_project_dir: Path = project_dir or pipelines_root.parent
    selected_node_count = 0
    try:
        client.validate_metadata_state(database)
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=pipelines_root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
        )
        compiled_pipelines: tuple[CompiledPipeline, ...] = analysis.compiled_project.pipelines
        loaded_tests: tuple[LoadedSqlTest, ...] = analysis.compiled_project.tests
        selected_tests: tuple[LoadedSqlTest, ...] = select_loaded_sql_tests(
            loaded_tests=loaded_tests,
            compiled_pipelines=compiled_pipelines,
            selectors=selectors,
            paths=paths,
            project_dir=resolved_project_dir,
        )
        selected_node_count = len(selected_tests)
        if not selected_tests:
            print("No SQL tests found.")
            invocation: AdapterInvocationRecord = build_invocation_record(
                started=started,
                terminal=TerminalInvocation(
                    project_dir=resolved_project_dir,
                    target_identity=database,
                    command="test",
                    mode=None,
                    outcome="succeeded",
                    exit_code=0,
                    materialized_outcome=None,
                    deployment_id=None,
                    workflow_id=None,
                    selected_node_count=0,
                    error_message=None,
                    summary={"failed_count": 0},
                ),
            )
            _ = persist_terminal_observations(
                client=client,
                database=database,
                invocation=invocation,
                node_results=(),
            )
            return 0
        selected_test_keys: frozenset[tuple[Path, int]] = frozenset(
            (loaded_test.file_path, loaded_test.test_index) for loaded_test in selected_tests
        )
        test_cases: tuple[SqlTestCase, ...] = tuple(
            test_case
            for test_case in analysis.compiled_project.test_cases
            if (test_case.file_path, test_case.test_index) in selected_test_keys
        )
        results: tuple[SqlTestExecutionResult, ...] = execute_sql_tests(
            test_cases=test_cases,
            client=client,
        )
        write_test_runtime_target(
            target_dir=target_dir or (resolved_project_dir / DEFAULT_TARGET_DIRECTORY_NAME),
            test_cases=test_cases,
            results=results,
        )
        rendered_output: str = render_sql_test_results(
            results=results,
            project_dir=resolved_project_dir,
            verbose=verbose,
        )
        print(rendered_output)
    except Exception as error:
        failed_invocation: AdapterInvocationRecord = build_invocation_record(
            started=started,
            terminal=TerminalInvocation(
                project_dir=resolved_project_dir,
                target_identity=database,
                command="test",
                mode=None,
                outcome="failed",
                exit_code=1,
                materialized_outcome=None,
                deployment_id=None,
                workflow_id=None,
                selected_node_count=selected_node_count,
                error_message=str(error),
                summary={"failed_before_results": True},
            ),
        )
        _ = persist_terminal_observations(
            client=client,
            database=database,
            invocation=failed_invocation,
            node_results=(),
        )
        raise
    failed_count: int = sum(1 for result in results if not result.passed)
    exit_code: int = 1 if failed_count else 0
    invocation = build_invocation_record(
        started=started,
        terminal=TerminalInvocation(
            project_dir=resolved_project_dir,
            target_identity=database,
            command="test",
            mode=None,
            outcome="failed" if exit_code else "succeeded",
            exit_code=exit_code,
            materialized_outcome=None,
            deployment_id=None,
            workflow_id=None,
            selected_node_count=len(test_cases),
            error_message=None,
            summary={"failed_count": failed_count},
        ),
    )
    node_results: tuple[AdapterNodeResultRecord, ...] = tuple(
        _test_node_result(
            invocation=invocation,
            test_case=test_case,
            result=result,
        )
        for test_case, result in zip(test_cases, results, strict=True)
    )
    _ = persist_terminal_observations(
        client=client,
        database=database,
        invocation=invocation,
        node_results=node_results,
    )
    return exit_code


def _test_node_result(
    *,
    invocation: AdapterInvocationRecord,
    test_case: SqlTestCase,
    result: SqlTestExecutionResult,
) -> AdapterNodeResultRecord:
    missing_count: int = sum(len(target.missing_rows) for target in result.target_results)
    unexpected_count: int = sum(len(target.unexpected_rows) for target in result.target_results)
    status: str = (
        "error" if result.error_message is not None else ("passed" if result.passed else "failed")
    )
    return build_node_result_record(
        invocation=invocation,
        identity=_native.require_quality_identity(test_case.quality_identity),
        context=QualityResultContext(trigger=QualityResultTrigger.MANUAL),
        status=status,
        severity=None,
        failure_count=missing_count + unexpected_count + int(result.error_message is not None),
        payload={
            "missing_count": missing_count,
            "unexpected_count": unexpected_count,
            "targets": _target_payloads(result),
        },
        error_message=result.error_message,
    )


def _target_payloads(result: SqlTestExecutionResult) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    target: SqlTestTargetExecutionResult
    for target in result.target_results:
        payloads.append(
            {
                "name": target.target_model_name,
                "missing_rows": [list(row) for row in target.missing_rows[:5]],
                "unexpected_rows": [list(row) for row in target.unexpected_rows[:5]],
            }
        )
    return payloads
