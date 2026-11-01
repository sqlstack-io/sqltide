"""Assembly helpers for SQL-native test cases."""

from __future__ import annotations

from dataclasses import replace

from sqltide._native import SqlTestChainRegistry
from sqltide.adapter.types import AdapterSetDifferenceComparisonRenderer
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.quality.main._build_test_quality_identity import (
    build_test_quality_identity,
)
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.compiler.test_discovery.models import (
    LoadedSqlTest,
    SqlTestMacroPayload,
    SqlTestModelPayload,
)
from sqltide.compiler.test_discovery.types import SqlTestMode
from sqltide.compiler.testing._helpers.comparison import render_comparison_query
from sqltide.compiler.testing._helpers.macro_assembly import build_macro_target
from sqltide.compiler.testing._helpers.model_assembly import (
    build_assertion_steps,
    build_chain_steps,
)
from sqltide.compiler.testing.classes.sql_test_chain_assembler import SqlTestChainAssembler
from sqltide.compiler.testing.exceptions import SqlTestAssemblyError
from sqltide.compiler.testing.models import (
    SqlTestAssertionStep,
    SqlTestCase,
    SqlTestChainStep,
)


def build_sql_test_case(
    *,
    loaded_test: LoadedSqlTest,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    reference_rewriter: SqlReferenceRewriter,
    comparison_renderer: AdapterSetDifferenceComparisonRenderer,
    dialect: str,
    chain_registry: SqlTestChainRegistry,
) -> SqlTestCase:
    """Assemble one discovered SQL test into an executable comparison statement."""

    if loaded_test.mode == SqlTestMode.MACRO:
        return _build_macro_test_case(
            loaded_test=loaded_test,
            comparison_renderer=comparison_renderer,
            dialect=dialect,
        )
    return _build_model_test_case(
        loaded_test=loaded_test,
        compiled_pipelines=compiled_pipelines,
        reference_rewriter=reference_rewriter,
        comparison_renderer=comparison_renderer,
        dialect=dialect,
        chain_registry=chain_registry,
    )


def _build_model_test_case(
    *,
    loaded_test: LoadedSqlTest,
    compiled_pipelines: tuple[CompiledPipeline, ...],
    reference_rewriter: SqlReferenceRewriter,
    comparison_renderer: AdapterSetDifferenceComparisonRenderer,
    dialect: str,
    chain_registry: SqlTestChainRegistry,
) -> SqlTestCase:
    payload: SqlTestModelPayload = _require_model_payload(loaded_test)
    assembler: SqlTestChainAssembler = SqlTestChainAssembler(
        loaded_test=loaded_test,
        payload=payload,
        compiled_pipelines=compiled_pipelines,
        reference_rewriter=reference_rewriter,
        chain_registry=chain_registry,
    )
    authored_ctes: tuple[tuple[str, str], ...] = tuple(
        (cte.name, cte.query) for cte in loaded_test.authored_ctes
    )
    target_cases: tuple[SqlTestChainStep, ...] = build_chain_steps(
        loaded_test=loaded_test,
        payload=payload,
        assembler=assembler,
        authored_ctes=authored_ctes,
        dialect=dialect,
    )
    assertion_cases: tuple[SqlTestAssertionStep, ...] = build_assertion_steps(
        loaded_test=loaded_test,
        payload=payload,
        assembler=assembler,
        authored_ctes=authored_ctes,
        dialect=dialect,
    )
    test_case: SqlTestCase = SqlTestCase(
        file_path=loaded_test.file_path,
        query=render_comparison_query(
            comparison_renderer=comparison_renderer,
            target_cases=target_cases,
            assertion_cases=assertion_cases,
        ),
        target_cases=target_cases,
        assertion_cases=assertion_cases,
        warnings=assembler.unreachable_mock_warnings(),
        test_index=loaded_test.test_index,
        name=loaded_test.name,
    )
    return replace(
        test_case,
        quality_identity=build_test_quality_identity(
            loaded_test=loaded_test,
            test_case=test_case,
            dialect=dialect,
        ),
    )


def _build_macro_test_case(
    *,
    loaded_test: LoadedSqlTest,
    comparison_renderer: AdapterSetDifferenceComparisonRenderer,
    dialect: str,
) -> SqlTestCase:
    payload: SqlTestMacroPayload = _require_macro_payload(loaded_test)
    target_cases: tuple[SqlTestChainStep, ...] = (
        build_macro_target(loaded_test=loaded_test, payload=payload, dialect=dialect),
    )
    test_case: SqlTestCase = SqlTestCase(
        file_path=loaded_test.file_path,
        query=render_comparison_query(
            comparison_renderer=comparison_renderer,
            target_cases=target_cases,
            assertion_cases=(),
        ),
        target_cases=target_cases,
        test_index=loaded_test.test_index,
        name=loaded_test.name,
    )
    return replace(
        test_case,
        quality_identity=build_test_quality_identity(
            loaded_test=loaded_test,
            test_case=test_case,
            dialect=dialect,
        ),
    )


def _require_model_payload(loaded_test: LoadedSqlTest) -> SqlTestModelPayload:
    payload: object = loaded_test.payload
    if isinstance(payload, SqlTestModelPayload):
        return payload
    raise SqlTestAssemblyError(f"SQL test '{loaded_test.file_path}' is not a model-mode test")


def _require_macro_payload(loaded_test: LoadedSqlTest) -> SqlTestMacroPayload:
    payload: object = loaded_test.payload
    if isinstance(payload, SqlTestMacroPayload):
        return payload
    raise SqlTestAssemblyError(f"SQL test '{loaded_test.file_path}' is not a macro-mode test")
