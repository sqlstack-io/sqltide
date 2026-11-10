from pathlib import Path
from typing import cast

import pytest

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import KafkaLandingStep, LoadedPipeline
from tests.unit.src.sqltide.compiler.discovery._helpers.load.helpers import (
    write_pipeline_file,
)
from tests.unit.src.sqltide.compiler.discovery._test_types import (
    DiscoverPipelinesErrorTestCase,
    DiscoverPipelinesTestCase,
    PipelineNamingMacroRenameTestCase,
    PipelineNamingMacroTestCase,
    PipelinePrefixViolationTestCase,
    PipelineSourceInferenceErrorTestCase,
    PipelineSourceInferenceTestCase,
    ViewPipelineSourceInferenceTestCase,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import (
    write_project_toml,
    write_source_yml,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverPipelinesTestCase(
            description="finds example pipeline under pipelines root",
            pipelines_root=Path("tests/fixtures/basic_project/pipelines"),
            expected_pipeline_names=["pl__orders"],
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_root_when_discovering_pipelines_then_returns_loaded_pipelines(
    test_case: DiscoverPipelinesTestCase,
) -> None:
    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(test_case.pipelines_root)

    assert [
        pipeline.pipeline.name for pipeline in loaded_pipelines
    ] == test_case.expected_pipeline_names


@pytest.mark.parametrize(
    "test_case",
    [
        PipelinePrefixViolationTestCase(
            description="rejects an unprefixed pipeline directory under the default prefix",
            pipeline_directory_name="orders",
            expected_error_fragment="must start with configured naming.pipeline_prefix 'pl__'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_default_pipeline_prefix_when_directory_is_unprefixed_then_discovery_rejects_it(
    test_case: PipelinePrefixViolationTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "test"\ndefault_target = "test"\n[targets.test]\n',
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.pipeline_directory_name / "orders_view.sql",
        "MODEL (kind view); SELECT 1 AS order_id",
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.discover_pipelines(tmp_path / "pipelines")


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineNamingMacroTestCase(
            description="accepts a directory whose name matches the naming macro result",
            pipeline_directory_name="pl__orders",
            source_name="orders",
            model_name="orders_model",
            macro_source="""
from sqltide.compiler.macros.models import PipelineNamingContext

def pipeline_name(ctx: PipelineNamingContext) -> str:
    expected_context = (
        "pl__orders",
        "orders",
        ("orders_model",),
        "pipelines/pl__orders",
        "direct",
    )
    actual_context = (
        ctx.name,
        ctx.source_name,
        ctx.model_names,
        ctx.relative_directory,
        ctx.mode.value,
    )
    return ctx.name if actual_context == expected_context else "pl__unexpected"
""",
            expected_pipeline_names=["pl__orders"],
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_naming_macro_when_context_matches_then_pipeline_is_loaded(
    test_case: PipelineNamingMacroTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "test"
        default_target = "test"

        [naming]
        pipeline_naming_macro = "pipeline_name"

        [targets.test]
        """,
    )
    write_pipeline_file(tmp_path / "macros" / "pipeline_names.py", test_case.macro_source)
    write_source_yml(
        project_dir=tmp_path,
        relative_path="orders.yml",
        contents=f"""
        sources:
          - name: {test_case.source_name}
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {{mode: offsets}}
        """,
    )
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.pipeline_directory_name / f"{test_case.model_name}.sql",
        f'MODEL (); SELECT 1 AS order_id FROM __source("{test_case.source_name}")',
    )

    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(tmp_path / "pipelines")

    assert [
        loaded.pipeline.name for loaded in loaded_pipelines
    ] == test_case.expected_pipeline_names


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineNamingMacroRenameTestCase(
            description="reports the expected directory when the macro result differs",
            pipeline_directory_name="pl__orders",
            macro_source=(
                "from sqltide.compiler.macros.models import PipelineNamingContext\n\n"
                "def pipeline_name(ctx: PipelineNamingContext) -> str:\n"
                '    return "pl__expected"\n'
            ),
            expected_error_fragment="expected 'pipelines/pl__expected'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_naming_macro_when_expected_name_differs_then_discovery_reports_rename(
    test_case: PipelineNamingMacroRenameTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "test"
        default_target = "test"

        [naming]
        pipeline_naming_macro = "pipeline_name"

        [targets.test]
        """,
    )
    write_pipeline_file(tmp_path / "macros" / "pipeline_names.py", test_case.macro_source)
    write_pipeline_file(
        tmp_path / "pipelines" / test_case.pipeline_directory_name / "orders_view.sql",
        "MODEL (kind view); SELECT 1 AS order_id",
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.discover_pipelines(tmp_path / "pipelines")


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineSourceInferenceTestCase(
            description="infers one source through a model in another pipeline",
            project_files={
                "alpha/alpha_model.sql": """
                MODEL (order_by ["order_id"]);
                SELECT order_id::UInt64 AS order_id FROM __ref("orders")
                """,
                "alpha/bridge_model.sql": """
                MODEL (order_by ["order_id"]);
                SELECT order_id::UInt64 AS order_id FROM __ref("alpha_model")
                """,
                "beta/beta_model.sql": """
                MODEL (order_by ["order_id"]);
                SELECT order_id::UInt64 AS order_id FROM __ref("bridge_model")
                """,
            },
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
            """,
            expected_pipeline_sources=(("alpha", "orders"), ("beta", "orders")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transitive_driving_inputs_when_discovering_then_infers_pipeline_sources(
    test_case: PipelineSourceInferenceTestCase,
    tmp_path: Path,
) -> None:
    pipelines_root: Path = tmp_path / "pipelines"
    relative_path: str
    file_contents: str
    for relative_path, file_contents in test_case.project_files.items():
        write_pipeline_file(pipelines_root / relative_path, file_contents)
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[naming]\npipeline_prefix = ""\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents=test_case.source_contents,
    )

    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(pipelines_root)

    assert (
        tuple(
            (loaded.pipeline.name, cast(KafkaLandingStep, loaded.pipeline.source).name)
            for loaded in loaded_pipelines
        )
        == test_case.expected_pipeline_sources
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ViewPipelineSourceInferenceTestCase(
            description="ignores view upstreams when inferring a mixed pipeline source",
            project_files={
                "mixed/table_model.sql": (
                    'MODEL (); SELECT order_id::UInt64 AS order_id FROM __source("orders")'
                ),
                "mixed/terminal.sql": (
                    "MODEL (kind view); SELECT payment_id::UInt64 AS payment_id FROM "
                    '__source("payments")'
                ),
            },
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: payments
                kind: kafka
                broker_list: kafka:9092
                topic: source.payments
                replay_boundary: {mode: offsets}
            """,
            expected_pipeline_sources=(("mixed", "orders"),),
        ),
        ViewPipelineSourceInferenceTestCase(
            description="accepts a source-less view-only pipeline",
            project_files={
                "views/terminal.sql": (
                    "MODEL (kind view); SELECT order_id::UInt64 AS order_id FROM "
                    '__source("orders") JOIN __source("payments") ON 1 = 1'
                ),
            },
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: payments
                kind: kafka
                broker_list: kafka:9092
                topic: source.payments
                replay_boundary: {mode: offsets}
            """,
            expected_pipeline_sources=(("views", None),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_views_when_discovering_then_ignores_them_for_pipeline_source_inference(
    test_case: ViewPipelineSourceInferenceTestCase,
    tmp_path: Path,
) -> None:
    pipelines_root: Path = tmp_path / "pipelines"
    relative_path: str
    file_contents: str
    for relative_path, file_contents in test_case.project_files.items():
        write_pipeline_file(pipelines_root / relative_path, file_contents)
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[naming]\npipeline_prefix = ""\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents=test_case.source_contents,
    )

    loaded_pipelines: list[LoadedPipeline] = _native.discover_pipelines(pipelines_root)

    assert (
        tuple(
            (loaded.pipeline.name, getattr(loaded.pipeline.source, "name", None))
            for loaded in loaded_pipelines
        )
        == test_case.expected_pipeline_sources
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineSourceInferenceErrorTestCase(
            description="rejects a pipeline whose models resolve to multiple sources",
            project_files={
                "mixed/orders_model.sql": """
                MODEL (order_by ["order_id"]);
                SELECT order_id::UInt64 AS order_id FROM __ref("orders")
                """,
                "mixed/payments_model.sql": """
                MODEL (order_by ["payment_id"]);
                SELECT payment_id::UInt64 AS payment_id FROM __ref("payments")
                """,
            },
            source_contents="""
            sources:
              - name: orders
                kind: kafka
                broker_list: kafka:9092
                topic: source.orders
                replay_boundary: {mode: offsets}
              - name: payments
                kind: kafka
                broker_list: kafka:9092
                topic: source.payments
                replay_boundary: {mode: offsets}
            """,
            expected_error_fragment="must resolve to exactly one source; found orders, payments",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_multiple_source_roots_when_discovering_then_rejects_pipeline(
    test_case: PipelineSourceInferenceErrorTestCase,
    tmp_path: Path,
) -> None:
    pipelines_root: Path = tmp_path / "pipelines"
    relative_path: str
    file_contents: str
    for relative_path, file_contents in test_case.project_files.items():
        write_pipeline_file(pipelines_root / relative_path, file_contents)
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[naming]\npipeline_prefix = ""\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents=test_case.source_contents,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.discover_pipelines(pipelines_root)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverPipelinesErrorTestCase(
            description="raises value error when two pipelines define the same model name",
            project_files={
                "alpha/shared.sql": """
                MODEL (
                  engine "MergeTree()",
                  order_by ["order_id"],
                );

                SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
                """,
                "beta/shared.sql": """
                MODEL (
                  engine "MergeTree()",
                  order_by ["order_id"],
                );

                SELECT CAST(order_id AS UInt64) AS order_id FROM __ref("orders")
                """,
            },
            expected_error_type=ValueError,
            expected_error_fragment="Logical node name 'shared' is defined in both",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_duplicate_logical_names_when_discovering_then_it_raises_expected_error(
    test_case: DiscoverPipelinesErrorTestCase,
    tmp_path: Path,
) -> None:
    pipelines_root: Path = tmp_path / "pipelines"
    for relative_path, file_contents in test_case.project_files.items():
        write_pipeline_file(pipelines_root / relative_path, file_contents)
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "test"\ndefault_target = "test"\n'
            '[naming]\npipeline_prefix = ""\n[targets.test]\n'
        ),
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="sources.yml",
        contents="""
        sources:
          - name: orders
            kind: kafka
            broker_list: kafka:9092
            topic: source.orders
            replay_boundary: {mode: offsets}
        """,
    )

    with pytest.raises(test_case.expected_error_type, match=test_case.expected_error_fragment):
        _native.discover_pipelines(pipelines_root)


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
