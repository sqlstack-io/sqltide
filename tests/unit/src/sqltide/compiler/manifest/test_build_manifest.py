from collections.abc import Iterator
from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide._native import analyze_project
from sqltide.adapter.models import AdapterManifest, AdapterManifestResource
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.cli.entry._helpers.compiler_profile import build_compiler_adapter_profile
from sqltide.compiler.manifest.main.build_manifest import build_manifest
from sqltide.compiler.pipeline.models import CompileAnalysis
from tests.unit.src.sqltide.compiler.manifest._test_types import (
    ManifestBuildTestCase,
    ManifestFingerprintTestCase,
    SharedSourceManifestTestCase,
)
from tests.unit.src.sqltide.compiler.pipeline.helpers import (
    write_compilation_project,
    write_shared_source_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestBuildTestCase(
            description="complete project manifest",
            expected_pipelines=("pl__alpha", "pl__zeta"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_complete_project_when_building_manifest_then_records_every_pipeline_and_resource(
    tmp_path: Path,
    test_case: ManifestBuildTestCase,
) -> None:
    write_compilation_project(tmp_path)
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=_native.load_project_input_for_path(path=tmp_path),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    manifest: AdapterManifest = build_manifest(
        analysis=analysis,
        invocation_id="invocation-1",
        project_identity="project",
        target_database="analytics",
        tool_version="test",
        project_revision="revision-1",
        published_at="2026-08-28 08:00:00.000000",
    )

    assert manifest.pipelines == test_case.expected_pipelines
    assert {resource.pipeline_name for resource in manifest.resources} == {
        "pl__alpha",
        "pl__zeta",
    }
    assert {resource.logical_name for resource in manifest.resources} == {
        "alpha_model",
        "alpha_source",
        "zeta_model",
        "zeta_source",
    }
    assert all(resource.resource_database == "analytics" for resource in manifest.resources)
    assert manifest.target_name == "test"
    assert manifest.project_revision == "revision-1"


@pytest.mark.parametrize(
    "test_case",
    [
        ManifestFingerprintTestCase(
            description="equivalent project manifests",
            expected_equal=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_equivalent_project_when_building_manifest_twice_then_content_fingerprint_is_stable(
    tmp_path: Path,
    test_case: ManifestFingerprintTestCase,
) -> None:
    write_compilation_project(tmp_path)
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=_native.load_project_input_for_path(path=tmp_path),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    first: AdapterManifest = build_manifest(
        analysis=analysis,
        invocation_id="invocation-1",
        project_identity="project",
        target_database="analytics",
        tool_version="test",
    )
    second: AdapterManifest = build_manifest(
        analysis=analysis,
        invocation_id="invocation-2",
        project_identity="project",
        target_database="analytics",
        tool_version="test",
    )

    assert first.manifest_id != second.manifest_id
    assert (first.manifest_fingerprint == second.manifest_fingerprint) is test_case.expected_equal
    assert first.pipelines == second.pipelines
    assert first.resources == second.resources


@pytest.mark.parametrize(
    "test_case",
    [
        SharedSourceManifestTestCase(
            description="project source consumed by multiple pipelines",
            expected_pipeline_names=frozenset(("pl__alpha", "pl__zeta")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_shared_project_source_when_building_manifest_then_attributes_every_consumer(
    tmp_path: Path,
    test_case: SharedSourceManifestTestCase,
) -> None:
    write_shared_source_project(tmp_path)
    analysis: CompileAnalysis = analyze_project(
        pipelines_root=tmp_path / "pipelines",
        loaded_project=_native.load_project_input_for_path(path=tmp_path),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    )

    manifest: AdapterManifest = build_manifest(
        analysis=analysis,
        invocation_id="invocation-1",
        project_identity="shared_source_project",
        target_database="analytics",
        tool_version="test",
    )

    source_resources: Iterator[AdapterManifestResource] = filter(
        lambda resource: resource.logical_name == "orders", manifest.resources
    )
    source_pipeline_names: frozenset[str] = frozenset(
        resource.pipeline_name for resource in source_resources
    )
    assert source_pipeline_names == test_case.expected_pipeline_names
