"""Recursive real-model chain assembly for one SQL-native test."""

from __future__ import annotations

from collections.abc import Mapping

import sqltide._native as _native
from sqltide.compiler.compile.main.replace_refs import replace_refs
from sqltide.compiler.compile.models import CompiledModel, CompiledPipeline, ParsedRef
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.compiler.test_discovery.constants import (
    EXPECTED_CTE_PREFIX,
    REF_CTE_PREFIX,
    SOURCE_CTE_PREFIX,
)
from sqltide.compiler.test_discovery.models import (
    LoadedSqlTest,
    SqlTestModelPayload,
)
from sqltide.compiler.testing.constants import ASSEMBLED_MODEL_CTE_PREFIX, CHAIN_CYCLE_REASON
from sqltide.compiler.testing.exceptions import SqlTestAssemblyError


class SqlTestChainAssembler:
    """Resolve logical relations into mock CTEs or real compiled model CTEs."""

    def __init__(
        self,
        *,
        loaded_test: LoadedSqlTest,
        payload: SqlTestModelPayload,
        compiled_pipelines: tuple[CompiledPipeline, ...],
        reference_rewriter: SqlReferenceRewriter,
        chain_registry: _native.SqlTestChainRegistry | None = None,
    ) -> None:
        self._loaded_test: LoadedSqlTest = loaded_test
        self._payload: SqlTestModelPayload = payload
        self._reference_rewriter: SqlReferenceRewriter = reference_rewriter
        self._registry: dict[str, CompiledModel] = _build_registry(compiled_pipelines)
        self._source_names: frozenset[str] = frozenset(
            compiled_pipeline.source.key.name
            for compiled_pipeline in compiled_pipelines
            if compiled_pipeline.source is not None
        )
        self._mock_cte_by_name: dict[str, str] = {
            mock.name: mock.cte_name for mock in payload.mocks
        }
        self._chain_registry: _native.SqlTestChainRegistry = (
            build_chain_registry(compiled_pipelines) if chain_registry is None else chain_registry
        )
        self._assembled_cte_by_name: dict[str, str] = {}
        self._assembled_position_by_name: dict[str, int] = {}
        self._assembled_ctes: list[tuple[str, str]] = []
        self._used_mock_cte_names: set[str] = set()

    @property
    def registry(self) -> Mapping[str, CompiledModel]:
        """Return every compiled model addressable by this test."""

        return self._registry

    @property
    def assembled_ctes(self) -> tuple[tuple[str, str], ...]:
        """Return real model CTEs in dependency order."""

        return tuple(self._assembled_ctes)

    def assembled_position(self, logical_name: str) -> int:
        """Return the dependency-order position of one assembled model."""

        return self._assembled_position_by_name[logical_name]

    def unreachable_mock_warnings(self) -> tuple[str, ...]:
        """Report authored mocks that no assembled relation ever reached."""

        return tuple(
            f"SQL test '{self._loaded_test.file_path}' never reaches mock '{mock.cte_name}'"
            for mock in self._payload.mocks
            if mock.cte_name not in self._used_mock_cte_names
        )

    def resolve(self, *, logical_name: str) -> str:
        """Return the CTE name that realizes one logical relation."""

        try:
            assembled_names, used_mock_names = self._chain_registry.resolve(
                logical_name, set(self._mock_cte_by_name), set(self._assembled_cte_by_name)
            )
        except ValueError as error:
            reason, name = error.args
            if reason == CHAIN_CYCLE_REASON:
                raise SqlTestAssemblyError(
                    f"SQL test '{self._loaded_test.file_path}' encountered a cyclic dependency "
                    f"while assembling '{name}'"
                ) from None
            raise SqlTestAssemblyError(self._unresolved_message(name)) from None
        self._used_mock_cte_names.update(
            self._mock_cte_by_name[mock_name] for mock_name in used_mock_names
        )
        assembled_name: str
        for assembled_name in assembled_names:
            self._assemble(logical_name=assembled_name, entry=self._registry[assembled_name])
        return self._resolved_cte_name(logical_name)

    def rewrite(self, *, sql: str, resolver: dict[str, str]) -> str:
        """Rewrite authored references onto assembled CTE names."""

        return replace_refs(sql=sql, resolver=resolver, rewriter=self._reference_rewriter)

    def _resolved_cte_name(self, logical_name: str) -> str:
        mock_cte_name: str | None = self._mock_cte_by_name.get(logical_name)
        if mock_cte_name is not None:
            return mock_cte_name
        return self._assembled_cte_by_name[logical_name]

    def _assemble(self, *, logical_name: str, entry: CompiledModel) -> None:
        resolver: dict[str, str] = {}
        parsed_ref: ParsedRef
        for parsed_ref in entry.parsed_refs:
            resolver[parsed_ref.name] = self._resolved_cte_name(parsed_ref.name)
        cte_name: str = f"{ASSEMBLED_MODEL_CTE_PREFIX}{logical_name}"
        self._assembled_position_by_name[logical_name] = len(self._assembled_ctes)
        self._assembled_ctes.append((cte_name, self.rewrite(sql=entry.query, resolver=resolver)))
        self._assembled_cte_by_name[logical_name] = cte_name

    def _unresolved_message(self, logical_name: str) -> str:
        suggestion_prefix: str = (
            SOURCE_CTE_PREFIX if logical_name in self._source_names else REF_CTE_PREFIX
        )
        target_model_names: str = ", ".join(
            cte.name.removeprefix(EXPECTED_CTE_PREFIX) for cte in self._payload.expected_targets
        )
        return (
            f"SQL test '{self._loaded_test.file_path}' targets "
            f"'{target_model_names}', but dependency '{logical_name}' "
            "cannot be resolved. Add "
            f"`{suggestion_prefix}{logical_name}` to mock it directly."
        )


def _build_registry(
    compiled_pipelines: tuple[CompiledPipeline, ...],
) -> dict[str, CompiledModel]:
    registry: dict[str, CompiledModel] = {}
    compiled_pipeline: CompiledPipeline
    for compiled_pipeline in compiled_pipelines:
        compiled_model: CompiledModel
        for compiled_model in compiled_pipeline.models:
            registry[compiled_model.key.name] = compiled_model
    return registry


def build_chain_registry(
    compiled_pipelines: tuple[CompiledPipeline, ...],
) -> _native.SqlTestChainRegistry:
    """Convert every compiled model's references once for all tests in one compile."""

    refs_by_model: dict[str, list[str]] = {}
    name: str
    model: CompiledModel
    for name, model in _build_registry(compiled_pipelines).items():
        refs_by_model[name] = [parsed_ref.name for parsed_ref in model.parsed_refs]
    return _native.SqlTestChainRegistry(refs_by_model)
