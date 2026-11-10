from pathlib import Path
from typing import cast

import pytest

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    KafkaLandingStep,
    LoadedPipeline,
    ReplayOnChangePolicy,
    TransformStep,
)
from sqltide.compiler.discovery.types import (
    BoundedReplayFallback,
    PipelineMode,
    ReplayOnChangeMode,
)
from tests.unit.src.sqltide.compiler.discovery._helpers.load._test_types import (
    InvalidPipelineModeTestCase,
    InvalidPipelineProtectionTestCase,
    LoadRegistryPipelineTestCase,
    LoadReplayPoliciesTestCase,
    MismatchedSourceTestCase,
    PipelineExecutionSettingsTestCase,
    PipelineModeTestCase,
    PipelineProtectionTestCase,
    RemovedPipelineSurfaceTestCase,
    StandaloneMacroOwnershipTestCase,
)
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_registry_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        LoadRegistryPipelineTestCase(
            description="resolves the registered source and discovered models",
            expected_pipeline_name="pl__orders",
            expected_source_name="orders",
            expected_transform_names=("orders_enriched",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_registry_project_when_loading_pipeline_then_it_resolves_source(
    test_case: LoadRegistryPipelineTestCase,
) -> None:
    loaded: LoadedPipeline = _native.load_pipeline_directory(
        Path("tests/fixtures/basic_project/pipelines/pl__orders")
    )

    assert loaded.pipeline.name == test_case.expected_pipeline_name
    source: KafkaLandingStep = cast(KafkaLandingStep, loaded.pipeline.source)
    assert source.name == test_case.expected_source_name
    assert (
        tuple(transform.name for transform in loaded.pipeline.transforms)
        == test_case.expected_transform_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineModeTestCase(
            description="direct mode overrides a virtual project default",
            configured_mode="direct",
            expected_mode=PipelineMode.DIRECT,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_mode_override_when_loading_then_it_overrides_project_default(
    test_case: PipelineModeTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents=f'mode = "{test_case.configured_mode}"',
        model_contents='MODEL (); SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )

    loaded: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    assert loaded.pipeline.mode == test_case.expected_mode


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineExecutionSettingsTestCase(
            description="loads replay settings under the pipeline execution phase",
            pipeline_config_contents="""
            [execution.replay.settings]
            max_threads = 8
            max_block_size = 64
            """,
            expected_replay_settings={"max_threads": "8", "max_block_size": "64"},
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_replay_settings_when_loading_then_retains_phase_defaults(
    test_case: PipelineExecutionSettingsTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents=test_case.pipeline_config_contents,
        model_contents='MODEL (); SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )

    loaded: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    assert (
        dict(loaded.pipeline.execution_settings.replay or {}) == test_case.expected_replay_settings
    )


@pytest.mark.parametrize(
    "test_case",
    [
        InvalidPipelineModeTestCase(
            description="rejects an unknown pipeline mode",
            configured_mode="shadow",
            expected_error_fragment="expected 'direct' or 'virtual'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_pipeline_mode_when_loading_then_it_reports_supported_values(
    test_case: InvalidPipelineModeTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents=f'mode = "{test_case.configured_mode}"',
        model_contents='MODEL (); SELECT order_id::UInt64 AS order_id FROM __source("orders")',
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.load_pipeline_directory(pipeline_dir)


@pytest.mark.parametrize(
    "test_case",
    [
        LoadReplayPoliciesTestCase(
            description="parses pipeline and model policy with renamed values",
            expected_pipeline_breaking_mode=ReplayOnChangeMode.BOUNDED,
            expected_pipeline_breaking_seconds=1800,
            expected_pipeline_non_breaking_mode=ReplayOnChangeMode.FULL,
            expected_pipeline_fallback=BoundedReplayFallback.BOUNDED_WITHOUT_HISTORY,
            expected_model_breaking_seconds=8,
            expected_model_fallback=BoundedReplayFallback.FULL,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_replay_policies_when_loading_then_it_uses_renamed_values(
    test_case: LoadReplayPoliciesTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents="""
        bounded_replay_fallback = "bounded_without_history"

        [replay_on_change]
        breaking = "bounded-30m"
        non_breaking = "full"
        """,
        model_contents="""
        MODEL (
          replay_on_change (breaking bounded-8s, non_breaking full),
          bounded_replay_fallback full,
        );
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
    )

    loaded: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)
    pipeline_policy: ReplayOnChangePolicy | None = loaded.pipeline.replay_on_change
    transform: TransformStep = cast(TransformStep, loaded.pipeline.transforms[0])
    model_policy: ReplayOnChangePolicy | None = transform.replay_on_change

    assert pipeline_policy is not None
    assert pipeline_policy.breaking is not None
    assert pipeline_policy.breaking.mode == test_case.expected_pipeline_breaking_mode
    assert pipeline_policy.breaking.lookback_seconds == test_case.expected_pipeline_breaking_seconds
    assert pipeline_policy.non_breaking is not None
    assert pipeline_policy.non_breaking.mode == test_case.expected_pipeline_non_breaking_mode
    assert loaded.pipeline.bounded_replay_fallback == test_case.expected_pipeline_fallback
    assert model_policy is not None
    assert model_policy.breaking is not None
    assert model_policy.breaking.lookback_seconds == test_case.expected_model_breaking_seconds
    assert transform.bounded_replay_fallback == test_case.expected_model_fallback


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineProtectionTestCase(
            description="empty protection uses pipeline-specific defaults",
            pipeline_name="orders",
            pipeline_config_contents='mode = "direct"\n[protection]',
            expected_warning=(
                "Pipeline 'orders' is protected. Confirm its operational impact before building."
            ),
            expected_confirmation="orders",
        ),
        PipelineProtectionTestCase(
            description="custom protection preserves the operator message and confirmation",
            pipeline_name="orders",
            pipeline_config_contents="""
            mode = "direct"
            [protection]
            warning = "Interrupts protected trading prices."
            confirmation = "DEPLOY_PROTECTED_PRICES"
            """,
            expected_warning="Interrupts protected trading prices.",
            expected_confirmation="DEPLOY_PROTECTED_PRICES",
        ),
        PipelineProtectionTestCase(
            description="empty protection makes an unsafe pipeline name shell safe",
            pipeline_name="order events",
            pipeline_config_contents='mode = "direct"\n[protection]',
            expected_warning=(
                "Pipeline 'order events' is protected. Confirm its operational impact before "
                "building."
            ),
            expected_confirmation="CONFIRM_order_events",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_protection_when_loading_then_resolves_operator_gate(
    test_case: PipelineProtectionTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_name=test_case.pipeline_name,
        pipeline_config_contents=test_case.pipeline_config_contents,
        model_contents="""
        MODEL ();
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
    )

    loaded: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    assert loaded.pipeline.protection is not None
    assert loaded.pipeline.protection.warning == test_case.expected_warning
    assert loaded.pipeline.protection.confirmation == test_case.expected_confirmation


@pytest.mark.parametrize(
    "test_case",
    [
        InvalidPipelineProtectionTestCase(
            description="rejects confirmation containing spaces",
            confirmation="DEPLOY ORDERS",
            expected_error_fragment="must contain only letters",
        ),
        InvalidPipelineProtectionTestCase(
            description="rejects confirmation containing shell metacharacters",
            confirmation="DEPLOY;ORDERS",
            expected_error_fragment="must contain only letters",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_protection_confirmation_when_loading_then_rejects_it(
    test_case: InvalidPipelineProtectionTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents=(
            f'mode = "direct"\n[protection]\nconfirmation = "{test_case.confirmation}"'
        ),
        model_contents="""
        MODEL ();
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
    )

    with pytest.raises(PipelineDiscoveryError) as error_info:
        _native.load_pipeline_directory(pipeline_dir)

    assert test_case.expected_error_fragment in str(error_info.value)


@pytest.mark.parametrize(
    "test_case",
    [
        RemovedPipelineSurfaceTestCase(
            description="rejects a redundant source key",
            pipeline_config_contents='source = "orders"',
            expected_error_fragment="unsupported keys: source",
        ),
        RemovedPipelineSurfaceTestCase(
            description="rejects a pipeline-owned boundary",
            pipeline_config_contents='replay_lineage_mode = "offsets"',
            expected_error_fragment="unsupported keys: replay_lineage_mode",
        ),
        RemovedPipelineSurfaceTestCase(
            description="rejects the old policy key",
            pipeline_config_contents='schema_change_backfill = "full"',
            expected_error_fragment="unsupported keys: schema_change_backfill",
        ),
        RemovedPipelineSurfaceTestCase(
            description="rejects the old bounded value",
            pipeline_config_contents='[replay_on_change]\nbreaking = "bounded(1h)"',
            expected_error_fragment="'full' or 'bounded-<duration>'",
        ),
        RemovedPipelineSurfaceTestCase(
            description="rejects the old fallback value",
            pipeline_config_contents='bounded_replay_fallback = "full_refresh"',
            expected_error_fragment="expected 'full' or 'bounded_without_history'",
        ),
        RemovedPipelineSurfaceTestCase(
            description="rejects protection on a virtual pipeline",
            pipeline_config_contents="[protection]",
            expected_error_fragment="only for direct pipelines",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_removed_pipeline_surface_when_loading_then_it_fails_with_source_diagnostic(
    test_case: RemovedPipelineSurfaceTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents=test_case.pipeline_config_contents,
        model_contents="""
        MODEL ();
        SELECT order_id::UInt64 AS order_id FROM __source("orders")
        """,
    )

    with pytest.raises(PipelineDiscoveryError) as error_info:
        _native.load_pipeline_directory(pipeline_dir)

    assert test_case.expected_error_fragment in str(error_info.value)
    assert str(pipeline_dir / "pipeline.toml") in str(error_info.value)


@pytest.mark.parametrize(
    "test_case",
    [
        MismatchedSourceTestCase(
            description="rejects a model driving from another source",
            model_source_name="customers",
            expected_error_fragment="references unknown driving input 'customers'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_wrong_model_source_when_loading_then_it_fails(
    test_case: MismatchedSourceTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents="",
        model_contents=(
            "MODEL ();\n"
            f'SELECT order_id::UInt64 AS order_id FROM __source("{test_case.model_source_name}")'
        ),
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.load_pipeline_directory(pipeline_dir)


@pytest.mark.parametrize(
    "test_case",
    [
        StandaloneMacroOwnershipTestCase(
            description="leaves project macro execution to compile input construction",
            macro_contents='raise RuntimeError("standalone loader executed macros")',
            expected_query=('SELECT @project_macro() AS order_id FROM __source("orders")'),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_macros_when_loading_one_pipeline_then_compile_phase_retains_ownership(
    test_case: StandaloneMacroOwnershipTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_project(
        project_dir=tmp_path,
        pipeline_config_contents="",
        model_contents=('MODEL ();\nSELECT @project_macro() AS order_id FROM __source("orders")'),
    )
    macro_file_path: Path = tmp_path / "macros" / "failed.py"
    macro_file_path.parent.mkdir(parents=True, exist_ok=True)
    macro_file_path.write_text(test_case.macro_contents, encoding="utf-8")

    loaded_pipeline: LoadedPipeline = _native.load_pipeline_directory(pipeline_dir)

    assert loaded_pipeline.pipeline.transforms[0].query == test_case.expected_query
