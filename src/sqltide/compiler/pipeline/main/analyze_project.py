"""Apache-2.0: SQLBuild cli/commands/_helpers/compile/pipeline.py@7e3b2f854f05."""

import time
from collections.abc import Mapping
from pathlib import Path

from sqltide.compiler.access.main._compile_access_policy import compile_access_policy
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.compile.main._assemble_project import assemble_project
from sqltide.compiler.compile.main._build_compile_inputs import build_compile_inputs
from sqltide.compiler.compile.models import (
    CompiledProject,
    CompileProjectInputs,
    CompilerAdapterProfile,
)
from sqltide.compiler.discovery.main._discover_project_inputs import (
    discover_project_inputs,
)
from sqltide.compiler.discovery.models import DiscoveredProjectInputs, LoadedProject
from sqltide.compiler.graph.main._build_project_graph import (
    build_project_graph_from_compiled_project,
)
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.pipeline._helpers.collection_tuning import deferred_cycle_collection
from sqltide.compiler.pipeline._helpers.sensor_names import reserved_sensor_names
from sqltide.compiler.pipeline._helpers.source_overrides import override_sources
from sqltide.compiler.pipeline.main._realize_project import realize_project
from sqltide.compiler.pipeline.models import (
    CompilationTimings,
    CompileAnalysis,
    RealizedProject,
)
from sqltide.compiler.rules.main._evaluate_project_rules import evaluate_project_rules
from sqltide.compiler.sql_analysis.classes.sql_model_analyzer import SqlModelAnalyzer
from sqltide.compiler.sql_analysis.classes.sql_reference_rewriter import (
    SqlReferenceRewriter,
)
from sqltide.diagnostics.main.attach_error_diagnostic import attach_error_diagnostic
from sqltide.diagnostics.models import SourceLocation
from sqltide.diagnostics.types import DiagnosticPhase
from sqltide.sensors.main.compile_sensors import compile_sensors
from sqltide.sensors.models import CompiledSensors


def analyze_project(
    *,
    pipelines_root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    enforce_rules: bool = True,
    source_overrides: Mapping[Path, str] | None = None,
    rule_selection: tuple[str, ...] | None = None,
) -> CompileAnalysis:
    """Discover, attach, compile, and graph one project without opening a connection."""

    discovery_start: int = time.monotonic_ns()
    try:
        discovered_inputs: DiscoveredProjectInputs = discover_project_inputs(
            pipelines_root=pipelines_root,
            loaded_project=loaded_project,
        )
        if source_overrides:
            discovered_inputs = override_sources(
                inputs=discovered_inputs, overrides=source_overrides
            )
    except Exception as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.DISCOVERY,
            code="SQT-DISCOVERY-001",
            location=_project_location(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
            ),
        )
        raise
    discovery_ms: int = _elapsed_ms(discovery_start)
    compile_inputs_start: int = time.monotonic_ns()
    try:
        compile_inputs: CompileProjectInputs = build_compile_inputs(
            discovered_inputs=discovered_inputs,
            adapter_profile=adapter_profile,
        )
        access_policy: CompiledAccessPolicy | None = compile_access_policy(
            source_file=discovered_inputs.access_file,
            pipeline_names=frozenset(loaded.pipeline.name for loaded in compile_inputs.pipelines),
        )
        sensors: CompiledSensors | None = compile_sensors(
            project_dir=pipelines_root.parent,
            reserved_names=reserved_sensor_names(compile_inputs=compile_inputs),
        )
    except Exception as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.DISCOVERY,
            code="SQT-DISCOVERY-001",
            location=_project_location(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
            ),
        )
        raise
    compile_inputs_ms: int = _elapsed_ms(compile_inputs_start)
    sql_analyzer: SqlModelAnalyzer = SqlModelAnalyzer(
        dialect=adapter_profile.type_inference_profile.sql_analysis_dialect
    )
    reference_rewriter: SqlReferenceRewriter = SqlReferenceRewriter(
        dialect=adapter_profile.sql_analysis_dialect
    )
    assembly_start: int = time.monotonic_ns()
    try:
        with deferred_cycle_collection():
            compiled_project: CompiledProject = assemble_project(
                inputs=compile_inputs,
                reference_rewriter=reference_rewriter,
                sql_analyzer=sql_analyzer,
            )
    except Exception as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.COMPILATION,
            code="SQT-COMPILE-001",
            location=_project_location(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
            ),
        )
        raise
    assembly_ms: int = _elapsed_ms(assembly_start)
    graph_start: int = time.monotonic_ns()
    try:
        graph: ProjectGraph = build_project_graph_from_compiled_project(project=compiled_project)
    except Exception as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.GRAPH,
            code="SQT-GRAPH-001",
            location=_project_location(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
            ),
        )
        raise
    graph_ms: int = _elapsed_ms(graph_start)
    realization_start: int = time.monotonic_ns()
    try:
        with deferred_cycle_collection():
            realized_project: RealizedProject = realize_project(
                project=compiled_project,
                adapter_profile=adapter_profile,
                sql_analyzer=sql_analyzer,
            )
    except Exception as error:
        _ = attach_error_diagnostic(
            error=error,
            phase=DiagnosticPhase.REALIZATION,
            code="SQT-REALIZATION-001",
            location=_project_location(
                pipelines_root=pipelines_root,
                loaded_project=loaded_project,
            ),
        )
        raise
    realization_ms: int = _elapsed_ms(realization_start)
    return evaluate_project_rules(
        analysis=CompileAnalysis(
            discovered_inputs=discovered_inputs,
            compile_inputs=compile_inputs,
            adapter_profile=adapter_profile,
            compiled_project=compiled_project,
            realized_project=realized_project,
            graph=graph,
            diagnostics=(),
            timings=CompilationTimings(
                discovery_ms=discovery_ms,
                compile_inputs_ms=compile_inputs_ms,
                assembly_ms=assembly_ms,
                graph_ms=graph_ms,
                realization_ms=realization_ms,
            ),
            access_policy=access_policy,
            sensors=sensors,
        ),
        enforce=enforce_rules,
        selection=rule_selection,
    )


def _elapsed_ms(start_ns: int) -> int:
    return (time.monotonic_ns() - start_ns) // 1_000_000


def _project_location(
    *, pipelines_root: Path, loaded_project: LoadedProject | None
) -> SourceLocation:
    path: Path = pipelines_root if loaded_project is None else loaded_project.source_file.file_path
    return SourceLocation(path=path, line=1, column=1)
