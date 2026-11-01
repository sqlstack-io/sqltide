"""Entry point for assembling discovered SQL tests into executable cases."""

from __future__ import annotations

from sqltide._native import SqlTestChainRegistry
from sqltide.adapter.types import AdapterSetDifferenceComparisonRenderer
from sqltide.compiler.compile.models import CompiledPipeline
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing._helpers.assembly import build_sql_test_case
from sqltide.compiler.testing.classes.sql_test_chain_assembler import build_chain_registry
from sqltide.compiler.testing.models import SqlTestCase


def build_sql_test_cases(
    *,
    loaded_tests: tuple[LoadedSqlTest, ...],
    compiled_pipelines: tuple[CompiledPipeline, ...],
    reference_rewriter: SqlReferenceRewriter,
    comparison_renderer: AdapterSetDifferenceComparisonRenderer,
    dialect: str,
) -> tuple[SqlTestCase, ...]:
    """Assemble discovered SQL tests into executable test queries."""

    chain_registry: SqlTestChainRegistry = build_chain_registry(compiled_pipelines)
    return tuple(
        build_sql_test_case(
            loaded_test=loaded_test,
            compiled_pipelines=compiled_pipelines,
            reference_rewriter=reference_rewriter,
            comparison_renderer=comparison_renderer,
            dialect=dialect,
            chain_registry=chain_registry,
        )
        for loaded_test in loaded_tests
    )
