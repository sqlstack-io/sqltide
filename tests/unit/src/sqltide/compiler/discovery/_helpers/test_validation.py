from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import (
    DiscoveredProjectFile,
    DiscoveredSourceFile,
    KafkaLandingStep,
    KafkaSettings,
    LoadedPipeline,
    Pipeline,
    ViewStep,
)
from tests.unit.src.sqltide.compiler.discovery._helpers._test_types import (
    GlobalNameCollisionTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        GlobalNameCollisionTestCase(
            description="rejects a pipeline that collides with a registered source name",
            pipeline_name="pl__orders",
            source_names=("pl__orders",),
            model_names=(),
            expected_error_fragment="Logical resource name 'pl__orders'",
        ),
        GlobalNameCollisionTestCase(
            description="rejects a pipeline that collides with one of its own model names",
            pipeline_name="pl__orders",
            source_names=(),
            model_names=("pl__orders",),
            expected_error_fragment="Logical node name 'pl__orders'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_shared_resource_name_when_validating_then_global_collision_is_rejected(
    test_case: GlobalNameCollisionTestCase,
) -> None:
    source_path: Path = Path("sources/orders.yml")
    sources: tuple[KafkaLandingStep, ...] = tuple(
        KafkaLandingStep(
            name=source_name,
            kafka=KafkaSettings(broker_list="kafka:9092", topic="source.orders"),
        )
        for source_name in test_case.source_names
    )
    source_files: tuple[DiscoveredSourceFile, ...] = tuple(
        DiscoveredSourceFile(
            source_file=DiscoveredProjectFile(
                file_path=source_path,
                relative_path=source_path,
                contents="",
            ),
            sources=sources,
        )
        for _ in range(min(len(sources), 1))
    )
    loaded_pipeline: LoadedPipeline = LoadedPipeline(
        pipeline=Pipeline(
            name=test_case.pipeline_name,
            source=None,
            transforms=tuple(
                ViewStep(name=model_name, query="SELECT 1") for model_name in test_case.model_names
            ),
        ),
        file_path=Path("pipelines") / test_case.pipeline_name,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        _native.validate_attached_project_inputs(
            source_files=source_files,
            loaded_project=None,
            loaded_pipelines=(loaded_pipeline,),
            loaded_tests=(),
            loaded_audits=(),
        )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
