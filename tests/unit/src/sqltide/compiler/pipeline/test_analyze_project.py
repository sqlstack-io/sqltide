import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from functools import partial
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

import sqltide._native as _native
from sqltide.adapter.models import AdapterManagedSource, AdapterTable
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.compile.main._run_compile import run_compile
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.main._build_compile_inputs import build_compile_inputs
from sqltide.compiler.compile.models import (
    CompiledTableModel,
    CompilerAdapterProfile,
    DesiredTable,
)
from sqltide.compiler.discovery._helpers.load import load_pipeline_directory
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.main._discover_project_inputs import (
    discover_project_inputs,
)
from sqltide.compiler.discovery.main.load_project_input_for_path import (
    load_project_input_for_path,
)
from sqltide.compiler.discovery.models import KafkaLandingStep, LoadedPipeline, LoadedProject
from sqltide.compiler.graph.main._build_project_graph import (
    build_project_graph_from_compiled_project,
)
from sqltide.compiler.macros.main._expand_macro_calls import expand_macro_calls
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.pipeline.models import CompileAnalysis
from tests.unit.src.sqltide.compiler.audit_discovery.helpers import write_sql_audit_file
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
    write_registry_project,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import write_project_toml
from tests.unit.src.sqltide.compiler.pipeline._test_types import (
    AnalysisDialectTestCase,
    AnalyzeProjectTestCase,
    CompilationEntrypointsTestCase,
    CompiledAuditPolicyTestCase,
    DestructionTargetMetadataTestCase,
    DuplicateProjectInputTestCase,
    ManagedSourceRetentionTestCase,
    ManagedSourceTtlPrecedenceTestCase,
    PrivateMacroDiscoveryTestCase,
    ProjectSqlAnalysisCallCountTestCase,
    ReadOnceCompilationTestCase,
    ReplayExecutionSettingsTestCase,
    ReplayPolicyModeErrorTestCase,
    SharedMacroRuntimeTestCase,
    SharedSourceRealizationTestCase,
)
from tests.unit.src.sqltide.compiler.pipeline.helpers import (
    assemble_project_with_completion,
    has_compilation_service_import,
    write_compilation_project,
    write_duplicate_test,
    write_macro_import_counter,
    write_managed_source_retention_project,
    write_managed_source_ttl_project,
    write_policy_validation_project,
    write_shared_source_project,
    write_source_model_name_collision,
)
from tests.unit.src.sqltide.compiler.test_discovery.helpers import model_payload


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionTargetMetadataTestCase(
            description="recompile reloads committed destruction safety policy",
            initial_limit="100GiB",
            fresh_limit="10GiB",
            expected_initial_bytes=107_374_182_400,
            expected_fresh_bytes=10_737_418_240,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_changed_destruction_policy_when_recompiling_then_fresh_limit_is_retained(
    tmp_path: Path,
    test_case: DestructionTargetMetadataTestCase,
) -> None:
    write_compilation_project(tmp_path)
    config_path: Path = tmp_path / "sqltide_project.toml"
    base_config: str = config_path.read_text()
    config_path.write_text(
        f"{base_config}\n[targets.test.destruction]\n"
        f'max_table_size_to_drop = "{test_case.initial_limit}"\n'
    )

    initial: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=load_project_input_for_path(path=tmp_path),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    config_path.write_text(
        f"{base_config}\n[targets.test.destruction]\n"
        f'max_table_size_to_drop = "{test_case.fresh_limit}"\n'
    )
    fresh: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=load_project_input_for_path(path=tmp_path),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    assert (
        initial.compiled_project.destruction_relation_drop_size_limit
        == test_case.expected_initial_bytes
    )
    assert (
        fresh.compiled_project.destruction_relation_drop_size_limit
        == test_case.expected_fresh_bytes
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayExecutionSettingsTestCase(
            description="model replay settings override pipeline defaults",
            expected_settings={"max_threads": "8", "max_block_size": "32"},
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_and_model_replay_settings_when_analyzing_then_model_values_win(
    test_case: ReplayExecutionSettingsTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents="""
        [execution.replay.settings]
        max_threads = 8
        max_block_size = 64
        """,
        model_contents="""
        MODEL (
          order_by ["order_id"],
          execution_settings (replay (max_block_size 32)),
        );
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
        pipeline_name="pl_orders",
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=tmp_path)

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=pipeline_dir.parent,
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    model: CompiledTableModel = cast(CompiledTableModel, analysis.compiled_project.models[0])

    assert dict(model.transform.execution_settings.replay or {}) == test_case.expected_settings


@pytest.mark.parametrize(
    "test_case",
    [
        CompiledAuditPolicyTestCase(
            description="pipeline policy overrides project defaults",
            audit_header='name "alpha quality"',
            expected_severity="error",
            expected_cadence_seconds=600,
            expected_warmup_seconds=1800,
            expected_scheduled=True,
        ),
        CompiledAuditPolicyTestCase(
            description="audit policy overrides inherited defaults",
            audit_header=('name "alpha quality", severity warning, every "2m", warmup "1m"'),
            expected_severity="warning",
            expected_cadence_seconds=120,
            expected_warmup_seconds=60,
            expected_scheduled=True,
        ),
        CompiledAuditPolicyTestCase(
            description="schedule opt out removes inherited cadence",
            audit_header='name "alpha quality", scheduled false',
            expected_severity="error",
            expected_cadence_seconds=None,
            expected_warmup_seconds=1800,
            expected_scheduled=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_policy_layers_when_compiling_then_effective_policy_is_explicit(
    test_case: CompiledAuditPolicyTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    write_project_toml(
        project_dir=project_dir,
        contents="""
        name = "compilation_project"
        default_target = "test"

        [defaults]
        pipeline_mode = \"virtual\"

        [defaults.audits]
        severity = "warning"
        every = "5m"
        warmup = "15m"

        [targets.test]
        database = "analytics"
        """,
    )
    write_pipeline_file(
        project_dir / "pipelines" / "pl__alpha" / "pipeline.toml",
        """
        [audit_defaults]
        severity = "error"
        every = "10m"
        warmup = "30m"
        """,
    )
    write_sql_audit_file(
        project_dir / "audits" / "quality" / "alpha_audit.sql",
        f"""
        AUDIT ({test_case.audit_header});
        SELECT order_id FROM __ref("alpha_model") WHERE order_id = 0
        """,
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    audit: LoadedSqlAudit = analysis.compiled_project.audits[0]

    assert audit.severity == test_case.expected_severity
    assert audit.cadence_seconds == test_case.expected_cadence_seconds
    assert audit.warmup_seconds == test_case.expected_warmup_seconds
    assert audit.scheduled is test_case.expected_scheduled


@pytest.mark.parametrize(
    "test_case",
    [
        AnalysisDialectTestCase(
            description="threads adapter profile dialect through reference rewriting",
            dialect="not-a-real-dialect",
            expected_error_fragment="could not be parsed with Polyglot",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adapter_profile_when_analyzing_then_references_use_its_dialect(
    test_case: AnalysisDialectTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    profile: CompilerAdapterProfile = replace(
        build_compiler_adapter_profile(ClickHouseAdapter()),
        sql_analysis_dialect=test_case.dialect,
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=profile,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectSqlAnalysisCallCountTestCase(
            description="bounds Python-side Polyglot calls across one two-model project analysis",
            expected_model_count=2,
            expected_analyze_calls=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_complete_project_when_analyzing_then_polyglot_calls_remain_bounded(
    test_case: ProjectSqlAnalysisCallCountTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    with (
        patch(
            "sqltide.compiler.sql_analysis._helpers.polyglot._native.polyglot_analyze_query",
            wraps=_native.polyglot_analyze_query,
        ) as analyze_query,
    ):
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )

    assert len(analysis.compiled_project.models) == test_case.expected_model_count
    assert analyze_query.call_count == test_case.expected_analyze_calls


@pytest.mark.parametrize(
    "test_case",
    [
        AnalyzeProjectTestCase(
            description="builds one immutable project analysis in stable order without connecting",
            expected_pipeline_names=("pl__alpha", "pl__zeta"),
            expected_graph_names=(
                "alpha_source",
                "alpha_model",
                "zeta_source",
                "zeta_model",
            ),
            expected_adapter_name="clickhouse",
            expected_dialect="clickhouse",
            expected_default_database="analytics",
            expected_source_file_count=8,
            expected_phase_call_count=1,
            expected_test_case_count=1,
            expected_assembly_realization_order=(
                "assembly",
                "assembly_completed",
                "realize_source",
                "realize_source",
                "realize_model",
                "realize_model",
            ),
            expected_logical_source_count=2,
            expected_logical_model_count=2,
            expected_source_resource_counts=(3, 3),
            expected_model_resource_counts=(2, 2),
            expected_macro_names=("identity_sql",),
            expected_macro_target_name="test",
            expected_macro_virtual_environments=True,
            expected_macro_variables=(),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_sources_when_analyzing_then_builds_one_stable_offline_result(
    test_case: AnalyzeProjectTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    connect: Mock = Mock(side_effect=AssertionError("compile opened a connection"))
    monkeypatch.setattr(ClickHouseAdapter, "connect", connect)
    adapter_profile: CompilerAdapterProfile = build_compiler_adapter_profile(ClickHouseAdapter())
    realize_source: Mock = Mock(wraps=adapter_profile.realize_source)
    model_relation_name: Mock = Mock(wraps=adapter_profile.model_relation_name)
    realize_model: Mock = Mock(wraps=adapter_profile.realize_model)
    assembly_completed: Mock = Mock()
    tracked_adapter_profile: CompilerAdapterProfile = replace(
        adapter_profile,
        realize_source=realize_source,
        model_relation_name=model_relation_name,
        realize_model=realize_model,
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    with (
        patch(
            "sqltide.compiler.pipeline.main.analyze_project.discover_project_inputs",
            wraps=discover_project_inputs,
        ) as discover_phase,
        patch(
            "sqltide.compiler.pipeline.main.analyze_project.build_compile_inputs",
            wraps=build_compile_inputs,
        ) as compile_inputs_phase,
        patch(
            "sqltide.compiler.pipeline.main.analyze_project.assemble_project",
            side_effect=partial(
                assemble_project_with_completion,
                completion=assembly_completed,
            ),
        ) as assembly_phase,
        patch(
            "sqltide.compiler.pipeline.main.analyze_project.build_project_graph_from_compiled_project",
            wraps=build_project_graph_from_compiled_project,
        ) as graph_phase,
    ):
        assembly_realization_calls: Mock = Mock()
        assembly_realization_calls.attach_mock(assembly_phase, "assembly")
        assembly_realization_calls.attach_mock(assembly_completed, "assembly_completed")
        assembly_realization_calls.attach_mock(realize_source, "realize_source")
        assembly_realization_calls.attach_mock(model_relation_name, "model_relation_name")
        assembly_realization_calls.attach_mock(realize_model, "realize_model")
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=tracked_adapter_profile,
        )

    assert (
        tuple(pipeline.pipeline.name for pipeline in analysis.compiled_project.pipelines)
        == test_case.expected_pipeline_names
    )
    assert tuple(key.name for key in analysis.graph.ordered_keys) == test_case.expected_graph_names
    assert analysis.adapter_profile.identity.name == test_case.expected_adapter_name
    assert analysis.adapter_profile.sql_analysis_dialect == test_case.expected_dialect
    assert (
        analysis.compile_inputs.effective_target.default_database
        == test_case.expected_default_database
    )
    assert analysis.compile_inputs.discovered_inputs is analysis.discovered_inputs
    assert analysis.compile_inputs.adapter_profile is analysis.adapter_profile
    assert analysis.graph.project is analysis.compiled_project
    assert analysis.realized_project.project is analysis.compiled_project
    assert len(analysis.compiled_project.sources) == test_case.expected_logical_source_count
    assert len(analysis.compiled_project.models) == test_case.expected_logical_model_count
    assert (
        tuple(
            len(analysis.realized_project.resources_by_logical_key[source.key])
            for source in analysis.compiled_project.sources
        )
        == test_case.expected_source_resource_counts
    )
    assert (
        tuple(
            len(analysis.realized_project.resources_by_logical_key[model.key])
            for model in analysis.compiled_project.models
        )
        == test_case.expected_model_resource_counts
    )
    assert len(analysis.compiled_project.test_cases) == test_case.expected_test_case_count
    assert tuple(analysis.compile_inputs.macro_registry.macros) == test_case.expected_macro_names
    assert analysis.compile_inputs.macro_context.target_name == test_case.expected_macro_target_name
    assert (
        analysis.compile_inputs.macro_context.virtual_environments
        is test_case.expected_macro_virtual_environments
    )
    assert tuple(analysis.compile_inputs.macro_context.variables.items()) == (
        test_case.expected_macro_variables
    )
    assert (
        analysis.compiled_project.test_cases[0].file_path
        == analysis.compiled_project.tests[0].file_path
    )
    assert analysis.discovered_inputs.loaded_project is loaded_project
    assert (
        1
        + len(
            (
                *(item.source_file for item in analysis.discovered_inputs.source_files),
                *analysis.discovered_inputs.model_files,
                *analysis.discovered_inputs.test_files,
                *analysis.discovered_inputs.audit_files,
                *analysis.discovered_inputs.macro_files,
            )
        )
        == test_case.expected_source_file_count
    )
    assert (
        min(
            analysis.timings.discovery_ms,
            analysis.timings.compile_inputs_ms,
            analysis.timings.assembly_ms,
            analysis.timings.graph_ms,
            analysis.timings.realization_ms,
        )
        >= 0
    )
    assert connect.call_count == 0
    assert discover_phase.call_count == test_case.expected_phase_call_count
    assert compile_inputs_phase.call_count == test_case.expected_phase_call_count
    assert assembly_phase.call_count == test_case.expected_phase_call_count
    assert graph_phase.call_count == test_case.expected_phase_call_count
    assert tuple(call[0] for call in assembly_realization_calls.mock_calls) == (
        test_case.expected_assembly_realization_order
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SharedSourceRealizationTestCase(
            description="realizes one reusable source independently of pipeline order",
            expected_logical_source_count=1,
            expected_model_count=2,
            expected_source_resource_count=3,
            expected_consumer_group="streambuild_shared_source_project_test_orders",
            expected_effective_consumer_group=(
                "streambuild_shared_source_project_test_orders_analytics"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_pipelines_share_source_when_analyzing_then_realizes_source_once(
    test_case: SharedSourceRealizationTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_shared_source_project(project_dir)
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)
    adapter: ClickHouseAdapter = ClickHouseAdapter()
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(adapter),
    )
    source_resources: tuple[object, ...] = analysis.realized_project.resources_by_logical_key[
        analysis.compiled_project.sources[0].key
    ]
    managed_source: AdapterManagedSource = cast(AdapterManagedSource, source_resources[0])

    assert len(analysis.compiled_project.sources) == test_case.expected_logical_source_count
    assert len(analysis.compiled_project.models) == test_case.expected_model_count
    assert len(source_resources) == test_case.expected_source_resource_count
    assert managed_source.consumer_group == test_case.expected_consumer_group
    rendered_source: str = adapter.render_resource(
        resource=managed_source,
        database=analysis.compile_inputs.effective_target.default_database or "",
    )
    assert f"kafka_group_name = '{test_case.expected_effective_consumer_group}'" in rendered_source
    immutable_graph_mapping: dict[object, tuple[object, ...]] = cast(
        dict[object, tuple[object, ...]], analysis.graph.upstream_edges_by_key
    )
    immutable_relation_mapping: dict[str, str] = cast(
        dict[str, str], analysis.realized_project.relation_name_by_logical_key
    )
    immutable_resource_mapping: dict[str, object] = cast(
        dict[str, object], analysis.realized_project.resources_by_logical_key
    )
    with pytest.raises(TypeError):
        immutable_graph_mapping["mutated"] = ()
    with pytest.raises(TypeError):
        immutable_relation_mapping["mutated"] = "mutated"
    with pytest.raises(TypeError):
        immutable_resource_mapping["mutated"] = "mutated"
    assert isinstance(analysis.compile_inputs.pipelines[0].pipeline.transforms, tuple)
    frozen_field_name: str = "discovery_ms"
    with pytest.raises(FrozenInstanceError):
        setattr(analysis.timings, frozen_field_name, -1)


@pytest.mark.parametrize(
    "test_case",
    [
        ManagedSourceTtlPrecedenceTestCase(
            description="uses the project default when a managed source omits TTL",
            project_default_ttl="_replay_landed_at + INTERVAL 7 DAY",
            source_ttl_declaration="",
            expected_landing_ttl="_replay_landed_at + INTERVAL 7 DAY",
        ),
        ManagedSourceTtlPrecedenceTestCase(
            description="uses the managed source TTL instead of the project default",
            project_default_ttl="_replay_landed_at + INTERVAL 7 DAY",
            source_ttl_declaration='ttl: "_replay_landed_at + INTERVAL 30 DAY"',
            expected_landing_ttl="_replay_landed_at + INTERVAL 30 DAY",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_managed_source_ttl_when_analyzing_then_applies_source_over_project_precedence(
    test_case: ManagedSourceTtlPrecedenceTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_managed_source_ttl_project(
        project_dir=project_dir,
        project_default_ttl=test_case.project_default_ttl,
        source_ttl_declaration=test_case.source_ttl_declaration,
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)
    loaded_pipeline: LoadedPipeline = load_pipeline_directory(
        project_dir / "pipelines" / "pl__orders"
    )

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    source_resources: tuple[object, ...] = analysis.realized_project.resources_by_logical_key[
        analysis.compiled_project.sources[0].key
    ]
    landing_table: AdapterTable = cast(AdapterTable, source_resources[1])
    desired_objects_by_name: dict[str, object] = {
        object_.name: object_ for object_ in analysis.realized_project.desired_state.objects
    }
    desired_landing_table: DesiredTable = cast(
        DesiredTable,
        desired_objects_by_name[landing_table.name],
    )
    standalone_source: KafkaLandingStep = cast(KafkaLandingStep, loaded_pipeline.pipeline.source)

    assert standalone_source.kafka.ttl == test_case.expected_landing_ttl
    assert landing_table.ttl == test_case.expected_landing_ttl
    assert desired_landing_table.ttl == test_case.expected_landing_ttl


@pytest.mark.parametrize(
    "test_case",
    [
        ManagedSourceRetentionTestCase(
            description="project Kafka retention uses broker time with safe landing fallback",
            source_retention_declaration="",
            expected_landing_ttl=(
                "least(ifNull(_replay_timestamp, _replay_landed_at), "
                "_replay_landed_at) + INTERVAL 7 DAY"
            ),
        ),
        ManagedSourceRetentionTestCase(
            description="source disabled retention overrides the project policy",
            source_retention_declaration="retention: false",
            expected_landing_ttl=None,
        ),
        ManagedSourceRetentionTestCase(
            description="source typed retention overrides the project policy",
            source_retention_declaration=(
                "retention: {duration: 2d, timestamp: broker, fallback: landed}"
            ),
            expected_landing_ttl=("ifNull(_replay_timestamp, _replay_landed_at) + INTERVAL 2 DAY"),
        ),
        ManagedSourceRetentionTestCase(
            description="broker retention defaults to a safe landing fallback",
            source_retention_declaration=("retention: {duration: 2d, timestamp: broker}"),
            expected_landing_ttl=("ifNull(_replay_timestamp, _replay_landed_at) + INTERVAL 2 DAY"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_typed_managed_source_retention_when_analyzing_then_renders_effective_ttl(
    test_case: ManagedSourceRetentionTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_managed_source_retention_project(
        project_dir=project_dir,
        source_retention_declaration=test_case.source_retention_declaration,
    )
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )
    source_resources: tuple[object, ...] = analysis.realized_project.resources_by_logical_key[
        analysis.compiled_project.sources[0].key
    ]
    landing_table: AdapterTable = cast(AdapterTable, source_resources[1])

    assert landing_table.ttl == test_case.expected_landing_ttl


@pytest.mark.parametrize(
    "test_case",
    [
        DuplicateProjectInputTestCase(
            description="rejects duplicate SQL test names across discovered files",
            expected_error_fragment="Duplicate SQL test name 'shared test' found in",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cross_file_duplicate_when_analyzing_then_rejects_aggregate(
    test_case: DuplicateProjectInputTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    write_duplicate_test(project_dir)

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        DuplicateProjectInputTestCase(
            description="rejects a model that collides with a project source identity",
            expected_error_fragment="Logical node name 'alpha_source' is defined in both",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_source_and_model_share_name_when_analyzing_then_rejects_logical_collision(
    test_case: DuplicateProjectInputTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    write_source_model_name_collision(project_dir)

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ReplayPolicyModeErrorTestCase(
            description="rejects project replay on change in direct mode",
            project_contents="""
            name = "policy_project"
            default_target = "test"
            [targets.test]
            database = "analytics"
            [defaults.replay_on_change]
            breaking = "full"
            """,
            local_contents="",
            pipeline_config_contents="",
            model_contents="""
            MODEL (order_by ["order_id"]);
            SELECT order_id::UInt64 AS order_id FROM __source("orders")
            """,
            expected_error_fragment="cannot define defaults.replay_on_change",
        ),
        ReplayPolicyModeErrorTestCase(
            description="rejects pipeline bounded fallback in direct mode",
            project_contents="""
            name = "policy_project"
            default_target = "test"
            [targets.test]
            database = "analytics"
            """,
            local_contents="",
            pipeline_config_contents='bounded_replay_fallback = "full"',
            model_contents="""
            MODEL (order_by ["order_id"]);
            SELECT order_id::UInt64 AS order_id FROM __source("orders")
            """,
            expected_error_fragment="cannot define bounded_replay_fallback",
        ),
        ReplayPolicyModeErrorTestCase(
            description="rejects model replay on change in direct mode",
            project_contents="""
            name = "policy_project"
            default_target = "test"
            [targets.test]
            database = "analytics"
            """,
            local_contents="",
            pipeline_config_contents="",
            model_contents="""
            MODEL (
              order_by ["order_id"],
              replay_on_change (breaking full),
            );
            SELECT order_id::UInt64 AS order_id FROM __source("orders")
            """,
            expected_error_fragment="cannot define replay_on_change",
        ),
        ReplayPolicyModeErrorTestCase(
            description="revalidates project policy after local switches mode to direct",
            project_contents="""
            name = "policy_project"
            default_target = "test"
            [defaults]
            pipeline_mode = \"virtual\"
            bounded_replay_fallback = "bounded_without_history"
            [targets.test]
            database = "analytics"
            """,
            local_contents="""
            [defaults]
            pipeline_mode = \"direct\"
            """,
            pipeline_config_contents="",
            model_contents="""
            MODEL (order_by ["order_id"]);
            SELECT order_id::UInt64 AS order_id FROM __source("orders")
            """,
            expected_error_fragment="cannot define defaults.bounded_replay_fallback",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_direct_mode_policy_when_analyzing_then_it_rejects_vde_only_setting(
    test_case: ReplayPolicyModeErrorTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_policy_validation_project(
        project_dir=project_dir,
        project_contents=test_case.project_contents,
        local_contents=test_case.local_contents,
        pipeline_config_contents=test_case.pipeline_config_contents,
        model_contents=test_case.model_contents,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=load_project_input_for_path(path=project_dir),
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilationEntrypointsTestCase(
            description="routes exactly ten command paths through one compilation service",
            expected_entrypoint_paths=(
                "src/sqltide/cli/audit/main/_run_audit.py",
                "src/sqltide/cli/build/main/_run_build.py",
                "src/sqltide/cli/compile/main/_run_compile.py",
                "src/sqltide/cli/destruction/_helpers/execution.py",
                "src/sqltide/cli/dev/main/_run_dev.py",
                "src/sqltide/cli/discover/main/_run_discover.py",
                "src/sqltide/cli/plan/main/_run_plan.py",
                "src/sqltide/cli/readiness/main/_run_deployment_audit.py",
                "src/sqltide/cli/reconcile/main/_run_reconcile.py",
                "src/sqltide/cli/test/main/_run_test.py",
            ),
            expected_entrypoint_count=10,
            expected_assembly_call_count=1,
            expected_realization_call_count=1,
            expected_consumer_rebuild_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cli_compilation_paths_when_inspecting_imports_then_only_one_service_is_used(
    test_case: CompilationEntrypointsTestCase,
) -> None:
    cli_root: Path = Path("src/sqltide/cli")
    entrypoint_paths: tuple[str, ...] = tuple(
        sorted(str(path) for path in filter(has_compilation_service_import, cli_root.rglob("*.py")))
    )
    service_call_counts: tuple[int, ...] = tuple(
        Path(path).read_text(encoding="utf-8").count("analyze_project(")
        for path in test_case.expected_entrypoint_paths
    )
    old_entrypoint_counts: tuple[int, ...] = tuple(
        Path(path).read_text(encoding="utf-8").count("discover_pipelines(")
        + Path(path).read_text(encoding="utf-8").count("compile_pipeline(")
        for path in test_case.expected_entrypoint_paths
    )
    assembly_source: str = Path("src/sqltide/compiler/compile/main/_assemble_project.py").read_text(
        encoding="utf-8"
    )
    analysis_source: str = Path("src/sqltide/compiler/pipeline/main/analyze_project.py").read_text(
        encoding="utf-8"
    )
    test_command_source: str = Path("src/sqltide/cli/test/main/_run_test.py").read_text(
        encoding="utf-8"
    )
    selection_source: str = Path("src/sqltide/cli/selection/main/_selection.py").read_text(
        encoding="utf-8"
    )

    assert entrypoint_paths == test_case.expected_entrypoint_paths
    assert len(entrypoint_paths) == test_case.expected_entrypoint_count
    assert service_call_counts == (1,) * test_case.expected_entrypoint_count
    assert old_entrypoint_counts == (0,) * test_case.expected_entrypoint_count
    assert assembly_source.count("build_sql_test_cases(") == test_case.expected_assembly_call_count
    assert analysis_source.count("realize_project(") == test_case.expected_realization_call_count
    assert analysis_source.index("assemble_project(") < analysis_source.index("realize_project(")
    assert assembly_source.count(".realize_model(") == test_case.expected_consumer_rebuild_count
    assert (
        test_command_source.count("build_sql_test_cases(")
        == test_case.expected_consumer_rebuild_count
    )
    assert (
        selection_source.count("build_desired_state(") == test_case.expected_consumer_rebuild_count
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SharedMacroRuntimeTestCase(
            description="shares one macro registry and context across models tests and audits",
            expected_expansion_call_count=4,
            expected_registry_identity=True,
            expected_context_identity=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_complete_project_when_expanding_macros_then_all_inputs_share_one_runtime(
    test_case: SharedMacroRuntimeTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    with (
        patch(
            "sqltide.compiler.discovery._helpers.model_sql.expand_macro_calls",
            wraps=expand_macro_calls,
        ) as model_expansion,
        patch(
            "sqltide.compiler.test_discovery._helpers.parsing.expand_macro_calls",
            wraps=expand_macro_calls,
        ) as test_expansion,
        patch(
            "sqltide.compiler.audit_discovery._helpers.parsing.expand_macro_calls",
            wraps=expand_macro_calls,
        ) as audit_expansion,
    ):
        analysis: CompileAnalysis = analyze_project(
            pipelines_root=project_dir / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
        )

    expansion_calls: tuple[Any, ...] = tuple(
        (
            *model_expansion.call_args_list,
            *test_expansion.call_args_list,
            *audit_expansion.call_args_list,
        )
    )
    expected_identities: tuple[bool, ...] = (True,) * test_case.expected_expansion_call_count

    assert len(expansion_calls) == test_case.expected_expansion_call_count
    assert (
        tuple(
            call.kwargs["registry"] is analysis.compile_inputs.macro_registry
            for call in expansion_calls
        )
        == expected_identities
    )
    assert (
        tuple(
            call.kwargs["context"] is analysis.compile_inputs.macro_context
            for call in expansion_calls
        )
        == expected_identities
    )
    assert (
        analysis.compiled_project.macro_registry is analysis.compile_inputs.macro_registry
    ) is test_case.expected_registry_identity
    assert (
        analysis.compiled_project.macro_context is analysis.compile_inputs.macro_context
    ) is test_case.expected_context_identity
    expanded_model_queries: tuple[str, ...] = (
        analysis.compile_inputs.pipelines[0].pipeline.transforms[0].query or "",
        analysis.compile_inputs.pipelines[1].pipeline.transforms[0].query or "",
    )
    assert tuple("@identity_sql" not in query for query in expanded_model_queries) == (
        True,
        True,
    )
    expected_test_query: str = (
        model_payload(analysis.compile_inputs.tests[0]).expected_targets[0].query
    )
    assert "@identity_sql" not in expected_test_query
    assert "CAST(1 AS UInt64)" in expected_test_query
    assert "@identity_sql" not in analysis.compile_inputs.audits[0].query
    assert "order_id = 0" in analysis.compile_inputs.audits[0].query


@pytest.mark.parametrize(
    "test_case",
    [
        PrivateMacroDiscoveryTestCase(
            description="excludes private modules and package initializers from macro loading",
            expected_macro_names=("identity_sql",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_private_macro_modules_when_analyzing_then_only_public_modules_are_loaded(
    test_case: PrivateMacroDiscoveryTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    private_macro_path: Path = project_dir / "macros" / "_private.py"
    private_macro_path.write_text("raise RuntimeError('private module loaded')\n", encoding="utf-8")
    initializer_path: Path = project_dir / "macros" / "__init__.py"
    initializer_path.write_text("raise RuntimeError('initializer loaded')\n", encoding="utf-8")
    loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)

    analysis: CompileAnalysis = analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=loaded_project,
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    assert tuple(analysis.compile_inputs.macro_registry.macros) == test_case.expected_macro_names


@pytest.mark.parametrize(
    "test_case",
    [
        ReadOnceCompilationTestCase(
            description="reads every authored source once through compile artifact writing",
            expected_relative_source_paths=(
                "audits/quality/alpha_audit.sql",
                "macros/formatting.py",
                "pipelines/pl__alpha/alpha_model.sql",
                "pipelines/pl__zeta/zeta_model.sql",
                "sources/alpha_source.yml",
                "sources/zeta_source.yml",
                "sqltide_project.toml",
                "tests/quality/alpha_test.sql",
            ),
            expected_exit_code=0,
            expected_macro_loader_read_count=0,
            expected_macro_import_count=1,
            expected_macro_names=("identity_sql",),
            expected_macro_relative_path="macros/formatting.py",
            expected_macro_source_fragment="def identity_sql(value: str) -> str:",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_compile_artifacts_when_writing_then_discovered_sources_are_not_reread(
    test_case: ReadOnceCompilationTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)
    macro_import_counter: Path = write_macro_import_counter(project_dir)
    original_read_text: Callable[..., str] = Path.read_text
    original_loader_get_data: Callable[..., bytes] = SourceFileLoader.get_data
    with (
        patch.object(
            Path,
            "read_text",
            autospec=True,
            side_effect=original_read_text,
        ) as read_text,
        patch.object(
            SourceFileLoader,
            "get_data",
            autospec=True,
            side_effect=original_loader_get_data,
        ) as loader_get_data,
    ):
        loaded_project: LoadedProject | None = load_project_input_for_path(path=project_dir)
        exit_code: int = run_compile(
            pipelines_root=project_dir / "pipelines",
            loaded_project=loaded_project,
            adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
            target_dir=project_dir / "target",
        )

    read_paths: tuple[Path, ...] = tuple(call.args[0] for call in read_text.call_args_list)
    relative_read_paths: tuple[str, ...] = tuple(
        sorted(str(path.relative_to(project_dir)) for path in read_paths)
    )
    loader_read_paths: tuple[Path, ...] = tuple(
        Path(call.args[1]) for call in loader_get_data.call_args_list
    )
    macro_loader_read_count: int = sum(
        path.is_relative_to(project_dir / "macros") for path in loader_read_paths
    )
    macro_import_count: int = len(macro_import_counter.read_text(encoding="utf-8").splitlines())
    manifest: dict[str, object] = json.loads(
        (project_dir / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    dag: dict[str, object] = json.loads(
        (project_dir / "target" / "sqltide_dag.json").read_text(encoding="utf-8")
    )
    manifest_macros: dict[str, dict[str, str]] = cast(dict[str, dict[str, str]], manifest["macros"])
    assert exit_code == test_case.expected_exit_code
    assert relative_read_paths == test_case.expected_relative_source_paths
    assert macro_loader_read_count == test_case.expected_macro_loader_read_count
    assert macro_import_count == test_case.expected_macro_import_count
    assert tuple(manifest_macros) == test_case.expected_macro_names
    assert manifest_macros["identity_sql"]["file"] == test_case.expected_macro_relative_path
    assert test_case.expected_macro_source_fragment in manifest_macros["identity_sql"]["source"]
    assert tuple(dag["macros"]) == test_case.expected_macro_names
