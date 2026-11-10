from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from sqltide._native import build_destruction_challenges, list_inactive_pipelines, plan_destruction
from sqltide.adapter.models import (
    AdapterDeploymentRecord,
    AdapterManifest,
    AdapterManifestResource,
    AdapterManifestSnapshot,
    AdapterMetadataObjectKey,
    AdapterPreparedObjectMapping,
    AdapterPublishEventRecord,
    AdapterStableBinding,
    CatalogRelation,
)
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.executor.destruction.exceptions import (
    DestructionDependencyError,
    DestructionExternalDependencyError,
    DestructionResourceError,
    DestructionSelectionError,
)
from sqltide.executor.destruction.models import (
    DestructionPlan,
    DestructionRelationEvidence,
    DestructionRequest,
    InactivePipeline,
)
from sqltide.executor.destruction.types import (
    DestructionOperation,
    DestructionOwnership,
)
from tests.unit.src.sqltide.executor.destruction._test_types import (
    DestructionChallengeTestCase,
    DestructionClosureTestCase,
    DestructionDependencyTestCase,
    DestructionDropLimitTestCase,
    DestructionDropOverrideTestCase,
    DestructionFrozenDropLimitTestCase,
    DuplicateSelectionTestCase,
    InactivePipelineDestructionTestCase,
    OrphanManifestTestCase,
    PipelineDestructionPlanTestCase,
    PlanningBehaviorTestCase,
    PreservedSourceClosureTestCase,
    StableDriftFingerprintTestCase,
    StableFingerprintTestCase,
    TargetResetPlanTestCase,
    UnsupportedManifestVersionTestCase,
    VirtualHistoryResetTestCase,
    WarehouseDependencyFailureTestCase,
)
from tests.unit.src.sqltide.executor.destruction.helpers import (
    PlanningFixture,
    build_model_dependency_planning_fixture,
    build_planning_fixture,
    build_source_dependency_planning_fixture,
    build_transitive_source_dependency_planning_fixture,
)

_NOW: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "test_case",
    [
        InactivePipelineDestructionTestCase(
            description="removed pipeline remains attributable through manifest history",
            expected_pipeline_name="retired",
            expected_logical_name="retired_orders",
            expected_manifest_relation_name="tbl__retired_orders",
            expected_physical_relation_name="tbl__retired_orders__deployment_1",
            expected_last_published_at="2026-08-23 10:00:00.000000",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_retained_manifest_when_listing_and_planning_then_inactive_pipeline_is_destroyable(
    test_case: InactivePipelineDestructionTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.catalog = replace(
        fixture.connection.catalog,
        relations=(
            *fixture.connection.catalog.relations,
            CatalogRelation(
                name=test_case.expected_manifest_relation_name, engine="View", columns=()
            ),
            CatalogRelation(
                name=test_case.expected_physical_relation_name,
                engine="MergeTree",
                columns=(),
            ),
        ),
    )
    fixture.connection.manifests = AdapterManifestSnapshot(
        status="available",
        manifests=(
            AdapterManifest(
                manifest_id="manifest-retired",
                invocation_id="invocation-retired",
                project_identity="commerce",
                target_name="uat",
                target_database="analytics",
                is_production=False,
                project_revision=None,
                manifest_fingerprint="retired-fingerprint",
                manifest_version=1,
                pipelines=("alpha", test_case.expected_pipeline_name),
                resources=(
                    AdapterManifestResource(
                        pipeline_name=test_case.expected_pipeline_name,
                        logical_type="model",
                        logical_name=test_case.expected_logical_name,
                        resource_role="stable_binding",
                        resource_database="analytics",
                        resource_name=test_case.expected_manifest_relation_name,
                        resource_kind="view",
                    ),
                ),
                tool_version="0.38.4",
                published_at=test_case.expected_last_published_at,
            ),
        ),
    )
    deployment: AdapterDeploymentRecord = fixture.connection.inventory.deployments[0]
    fixture.connection.inventory = replace(
        fixture.connection.inventory,
        deployments=(
            replace(
                deployment,
                prepared_object_mappings=(
                    *deployment.prepared_object_mappings,
                    AdapterPreparedObjectMapping(
                        logical_key=AdapterMetadataObjectKey(
                            database="analytics",
                            object_type="view",
                            name=test_case.expected_manifest_relation_name,
                        ),
                        physical_name=test_case.expected_physical_relation_name,
                        logical_model_name=test_case.expected_logical_name,
                    ),
                ),
            ),
        ),
    )
    request: DestructionRequest = DestructionRequest(
        operation="destroy_pipelines",
        target="uat",
        database="analytics",
        metadata_database="analytics",
        pipeline_names=(test_case.expected_pipeline_name,),
    )

    inactive: tuple[InactivePipeline, ...] = list_inactive_pipelines(
        request=replace(request, pipeline_names=()),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )
    plan: DestructionPlan = plan_destruction(
        request=request,
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=_NOW,
    )

    assert inactive == (
        InactivePipeline(
            name=test_case.expected_pipeline_name,
            model_count=1,
            resource_count=1,
            last_published_at=test_case.expected_last_published_at,
        ),
    )
    relations: dict[str, DestructionRelationEvidence] = {
        relation.name: relation for relation in plan.relations
    }
    assert plan.affected_pipeline_names == (test_case.expected_pipeline_name,)
    assert plan.affected_model_names == (test_case.expected_logical_name,)
    assert plan.include_orphans is True
    assert set(relations) == {
        test_case.expected_manifest_relation_name,
        test_case.expected_physical_relation_name,
    }
    assert all(
        relation.pipeline_names == (test_case.expected_pipeline_name,)
        for relation in relations.values()
    )


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="unrecorded pipeline remains outside destruction ownership",
            expected_value=r"Unknown pipelines: \('unowned',\)",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_manifest_ownership_when_planning_unknown_pipeline_then_request_is_rejected(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()

    with pytest.raises(DestructionSelectionError, match=test_case.expected_value):
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="analytics",
                pipeline_names=("unowned",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        OrphanManifestTestCase(
            description="selected pipeline historical orphan",
            expected_included_relation="legacy__orders",
            expected_excluded_relations=frozenset(
                ("vw__summary", "legacy__reassigned", "still__current")
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_orphans_when_planning_selected_pipeline_then_only_unreclaimed_are_included(
    test_case: OrphanManifestTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.catalog = replace(
        fixture.connection.catalog,
        relations=(
            *fixture.connection.catalog.relations,
            CatalogRelation(name="legacy__orders", engine="MergeTree", columns=()),
            CatalogRelation(name="legacy__reassigned", engine="MergeTree", columns=()),
            CatalogRelation(name="still__current", engine="MergeTree", columns=()),
        ),
    )
    fixture.connection.manifests = AdapterManifestSnapshot(
        status="available",
        manifests=(
            AdapterManifest(
                manifest_id="manifest-2",
                invocation_id="invocation-2",
                project_identity="commerce",
                target_name="uat",
                target_database="analytics",
                is_production=False,
                project_revision=None,
                manifest_fingerprint="fingerprint-2",
                manifest_version=1,
                pipelines=("alpha", "beta"),
                resources=(
                    AdapterManifestResource(
                        pipeline_name="beta",
                        logical_type="model",
                        logical_name="reassigned",
                        resource_role="primary",
                        resource_database="analytics",
                        resource_name="legacy__reassigned",
                        resource_kind="table",
                    ),
                    AdapterManifestResource(
                        pipeline_name="alpha",
                        logical_type="model",
                        logical_name="still_current",
                        resource_role="primary",
                        resource_database="analytics",
                        resource_name="still__current",
                        resource_kind="table",
                    ),
                ),
                tool_version="0.37.0",
                published_at="2026-08-21 10:00:00.000000",
            ),
            AdapterManifest(
                manifest_id="manifest-1",
                invocation_id="invocation-1",
                project_identity="commerce",
                target_name="uat",
                target_database="analytics",
                is_production=False,
                project_revision=None,
                manifest_fingerprint="fingerprint",
                manifest_version=1,
                pipelines=("alpha", "beta"),
                resources=(
                    AdapterManifestResource(
                        pipeline_name="alpha",
                        logical_type="model",
                        logical_name="reassigned",
                        resource_role="primary",
                        resource_database="analytics",
                        resource_name="legacy__reassigned",
                        resource_kind="table",
                    ),
                    AdapterManifestResource(
                        pipeline_name="alpha",
                        logical_type="model",
                        logical_name="legacy_orders",
                        resource_role="primary",
                        resource_database="analytics",
                        resource_name="legacy__orders",
                        resource_kind="table",
                    ),
                    AdapterManifestResource(
                        pipeline_name="alpha",
                        logical_type="model",
                        logical_name="old_summary",
                        resource_role="primary",
                        resource_database="analytics",
                        resource_name="vw__summary",
                        resource_kind="view",
                    ),
                ),
                tool_version="0.37.0",
                published_at="2026-08-20 10:00:00.000000",
            ),
        ),
    )

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation=DestructionOperation.DESTROY_PIPELINES,
            target="uat",
            database="analytics",
            metadata_database="analytics",
            pipeline_names=("alpha",),
            include_orphans=True,
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=_NOW,
    )

    relations: dict[str, DestructionRelationEvidence] = {
        relation.name: relation for relation in plan.relations
    }
    assert plan.include_orphans is True
    assert (
        DestructionOwnership.HISTORICAL_MANIFEST
        in relations[test_case.expected_included_relation].ownership
    )
    assert relations[test_case.expected_included_relation].pipeline_names == ("alpha",)
    assert test_case.expected_excluded_relations.isdisjoint(relations)


@pytest.mark.parametrize(
    "test_case",
    [
        UnsupportedManifestVersionTestCase(
            description="future manifest version",
            manifest_version=2,
            expected_error_match="Unsupported manifest version 2; expected 1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unsupported_manifest_when_including_orphans_then_planning_fails_closed(
    test_case: UnsupportedManifestVersionTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.manifests = AdapterManifestSnapshot(
        status="available",
        manifests=(
            AdapterManifest(
                manifest_id="future-manifest",
                invocation_id="invocation-1",
                project_identity="commerce",
                target_name="uat",
                target_database="analytics",
                is_production=False,
                project_revision=None,
                manifest_fingerprint="fingerprint",
                manifest_version=test_case.manifest_version,
                pipelines=("alpha",),
                resources=(),
                tool_version="future",
                published_at="2026-08-21 10:00:00.000000",
            ),
        ),
    )

    with pytest.raises(DestructionResourceError, match=test_case.expected_error_match):
        plan_destruction(
            request=DestructionRequest(
                operation=DestructionOperation.DESTROY_PIPELINES,
                target="uat",
                database="analytics",
                metadata_database="analytics",
                pipeline_names=("alpha",),
                include_orphans=True,
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionDropLimitTestCase(
            description="oversized existing relations block the complete plan",
            limit=1024,
            expected_resource_fragments=(
                "analytics.tbl__orders__deployment_1 (2,048 bytes)",
                "analytics.raw__events (4,096 bytes)",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_oversized_relation_when_planning_then_drop_limit_blocks_before_mutation(
    test_case: DestructionDropLimitTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.relation_drop_size_limit = test_case.limit

    with pytest.raises(DestructionResourceError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )

    assert f"{test_case.limit:,} bytes" in str(raised.value)
    assert all(fragment in str(raised.value) for fragment in test_case.expected_resource_fragments)


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionDropOverrideTestCase(
            description="finite destruction override permits relations above the server default",
            server_limit=1_024,
            override_limit=8_192,
            expected_effective_limit=8_192,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_finite_override_when_planning_then_it_replaces_server_drop_limit(
    test_case: DestructionDropOverrideTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.relation_drop_size_limit = test_case.server_limit
    fixture.connection.relation_drop_size_server_limit = test_case.server_limit
    analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=fixture.analysis.realized_project,
            graph=fixture.analysis.graph,
            compiled_project=SimpleNamespace(
                production_target=False,
                destruction_relation_drop_size_limit=test_case.override_limit,
            ),
        ),
    )

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=analysis,
        connection=fixture.connection,
        now=_NOW,
    )

    assert plan.relation_drop_size_limit == test_case.expected_effective_limit
    assert plan.relation_drop_size_server_limit == test_case.server_limit
    assert plan.relation_drop_size_override == test_case.override_limit


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionFrozenDropLimitTestCase(
            description="effective drop limit participates in frozen identity",
            first_limit=8_192,
            second_limit=16_384,
            expected_fingerprint_changed=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_drop_limit_change_when_planning_then_fingerprint_and_payload_change(
    test_case: DestructionFrozenDropLimitTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    first_analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=fixture.analysis.realized_project,
            graph=fixture.analysis.graph,
            compiled_project=SimpleNamespace(
                production_target=False,
                destruction_relation_drop_size_limit=test_case.first_limit,
            ),
        ),
    )
    first: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=first_analysis,
        connection=fixture.connection,
        now=_NOW,
    )
    second_analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=fixture.analysis.realized_project,
            graph=fixture.analysis.graph,
            compiled_project=SimpleNamespace(
                production_target=False,
                destruction_relation_drop_size_limit=test_case.second_limit,
            ),
        ),
    )
    second: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=second_analysis,
        connection=fixture.connection,
        now=_NOW,
    )

    assert first.relation_drop_size_limit == test_case.first_limit
    assert second.relation_drop_size_limit == test_case.second_limit
    assert (
        first.plan_fingerprint != second.plan_fingerprint
    ) is test_case.expected_fingerprint_changed


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionChallengeTestCase(
            description="one pipeline requires its exact name",
            pipeline_names=("alpha",),
            production_reset=False,
            expected_challenges=("alpha",),
        ),
        DestructionChallengeTestCase(
            description="three pipelines require all sorted names",
            pipeline_names=("charlie", "alpha", "beta"),
            production_reset=False,
            expected_challenges=("alpha", "beta", "charlie"),
        ),
        DestructionChallengeTestCase(
            description="larger sets require first middle and last sorted names",
            pipeline_names=("echo", "alpha", "delta", "bravo", "charlie"),
            production_reset=False,
            expected_challenges=("alpha", "charlie", "echo"),
        ),
        DestructionChallengeTestCase(
            description="production reset appends the production challenge",
            pipeline_names=("beta", "alpha"),
            production_reset=True,
            expected_challenges=("alpha", "beta", "PRODUCTION"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_affected_names_when_building_challenges_then_rules_are_deterministic(
    test_case: DestructionChallengeTestCase,
) -> None:
    challenges: tuple[str, ...] = build_destruction_challenges(
        pipeline_names=test_case.pipeline_names,
        production_reset=test_case.production_reset,
    )

    assert challenges == test_case.expected_challenges


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionDependencyTestCase(
            description="an unselected model downstream pipeline blocks destruction",
            fixture_builder=build_model_dependency_planning_fixture,
            expected_dependent_pipeline_names=("beta",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unselected_downstream_pipeline_when_planning_then_dependency_blocks(
    test_case: DestructionDependencyTestCase,
) -> None:
    fixture: PlanningFixture = test_case.fixture_builder()

    with pytest.raises(DestructionDependencyError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )

    assert raised.value.dependent_pipeline_names == test_case.expected_dependent_pipeline_names


@pytest.mark.parametrize(
    "test_case",
    [
        PreservedSourceClosureTestCase(
            description="shared managed source requires every consuming pipeline",
            expected_affected_pipeline_names=("beta",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_shared_managed_source_when_planning_then_other_consuming_pipeline_is_required(
    test_case: PreservedSourceClosureTestCase,
) -> None:
    fixture: PlanningFixture = build_source_dependency_planning_fixture()

    with pytest.raises(DestructionDependencyError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )

    assert raised.value.dependent_pipeline_names == test_case.expected_affected_pipeline_names


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionDependencyTestCase(
            description="shared-source requirements expand to a fixed point",
            fixture_builder=build_transitive_source_dependency_planning_fixture,
            expected_dependent_pipeline_names=("beta", "gamma"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_transitive_shared_sources_when_planning_then_complete_closure_is_required(
    test_case: DestructionDependencyTestCase,
) -> None:
    fixture: PlanningFixture = test_case.fixture_builder()

    with pytest.raises(DestructionDependencyError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )

    assert raised.value.dependent_pipeline_names == test_case.expected_dependent_pipeline_names


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionClosureTestCase(
            description="an explicit downstream closure unblocks destruction",
            expected_requested_pipeline_names=("alpha",),
            expected_included_pipeline_names=("beta",),
            expected_affected_pipeline_names=("alpha", "beta"),
            expected_affected_model_names=("orders", "summary"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_downstream_closure_when_planning_then_destruction_is_unblocked(
    test_case: DestructionClosureTestCase,
) -> None:
    fixture: PlanningFixture = build_model_dependency_planning_fixture()

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
            included_dependent_pipeline_names=("beta",),
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=_NOW,
    )

    assert plan.requested_pipeline_names == test_case.expected_requested_pipeline_names
    assert plan.included_dependent_pipeline_names == test_case.expected_included_pipeline_names
    assert plan.affected_pipeline_names == test_case.expected_affected_pipeline_names
    assert plan.affected_model_names == test_case.expected_affected_model_names


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineDestructionPlanTestCase(
            description="pipeline destruction contains its complete associated warehouse scope",
            expected_relation_names=(
                "kafka__events",
                "mv__events",
                "mv__orders",
                "old__orders__deployment_1",
                "raw__events",
                "raw__events__deployment_1",
                "tbl__orders",
                "tbl__orders__deployment_1",
            ),
            expected_excluded_relation_names=(
                "tbl__orders_backup",
                "external_users",
                "_sqltide_manifest_accident",
            ),
            expected_affected_source_names=("events",),
            expected_preserves_sources=False,
            expected_preserves_replay_data=False,
            expected_stable_ownership=(
                DestructionOwnership.CURRENT_MANIFEST,
                DestructionOwnership.PUBLISHED_STABLE_BINDING,
            ),
            expected_total_bytes=2048,
            expected_active_parts=2,
            expected_catalog_databases=["analytics"],
            expected_inventory_databases=["metadata"],
            expected_query_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_destruction_when_planning_then_complete_associated_scope_is_returned(
    test_case: PipelineDestructionPlanTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation=DestructionOperation.DESTROY_PIPELINES,
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=_NOW,
        plan_id="fixed-plan",
    )
    by_name: dict[str, DestructionRelationEvidence] = {
        relation.name: relation for relation in plan.relations
    }

    assert tuple(by_name) == test_case.expected_relation_names
    assert set(test_case.expected_excluded_relation_names).isdisjoint(by_name)
    assert plan.affected_source_names == test_case.expected_affected_source_names
    assert plan.preserves_sources is test_case.expected_preserves_sources
    assert plan.preserves_replay_data is test_case.expected_preserves_replay_data
    assert by_name["tbl__orders"].ownership == test_case.expected_stable_ownership
    assert by_name["tbl__orders__deployment_1"].total_bytes == test_case.expected_total_bytes
    assert by_name["tbl__orders__deployment_1"].active_parts == test_case.expected_active_parts
    assert fixture.connection.catalog_databases == test_case.expected_catalog_databases
    assert fixture.connection.inventory_databases == test_case.expected_inventory_databases
    assert len(fixture.connection.queries) == test_case.expected_query_count
    assert fixture.connection.catalog_match_count == 0
    with pytest.raises(FrozenInstanceError):
        setattr(plan, "".join(("tar", "get")), "other")


@pytest.mark.parametrize(
    "test_case",
    [
        TargetResetPlanTestCase(
            description="target reset includes managed sources but excludes adopted sources",
            expected_included_relation_names=(
                "kafka__events",
                "raw__events",
                "raw__events__deployment_1",
                "mv__events",
            ),
            expected_excluded_relation_names=(
                "external_users",
                "_sqltide_manifest_accident",
            ),
            expected_affected_source_names=("events",),
            expected_preserves_sources=False,
            expected_preserves_replay_data=False,
            expected_challenges=("alpha", "beta", "PRODUCTION"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_target_reset_when_planning_then_sources_include_managed_but_not_adopted(
    test_case: TargetResetPlanTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture(target_name="production")

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="reset_target",
            target="production",
            database="analytics",
            metadata_database="metadata",
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
        now=_NOW,
    )
    relation_names: tuple[str, ...] = tuple(relation.name for relation in plan.relations)

    assert set(test_case.expected_included_relation_names).issubset(relation_names)
    assert set(test_case.expected_excluded_relation_names).isdisjoint(relation_names)
    assert plan.affected_source_names == test_case.expected_affected_source_names
    assert plan.preserves_sources is test_case.expected_preserves_sources
    assert plan.preserves_replay_data is test_case.expected_preserves_replay_data
    assert plan.challenges == test_case.expected_challenges


@pytest.mark.parametrize(
    "test_case",
    [
        StableFingerprintTestCase(
            description="equivalent current state has stable fingerprints",
            expected_fingerprint_length=64,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_equivalent_current_state_when_replanning_then_fingerprints_are_stable(
    test_case: StableFingerprintTestCase,
) -> None:
    first_fixture: PlanningFixture = build_planning_fixture()
    second_fixture: PlanningFixture = build_planning_fixture()
    request: DestructionRequest = DestructionRequest(
        operation="destroy_pipelines",
        target="uat",
        database="analytics",
        metadata_database="metadata",
        pipeline_names=("alpha",),
    )

    first: DestructionPlan = plan_destruction(
        request=request,
        analysis=first_fixture.analysis,
        connection=first_fixture.connection,
        now=_NOW,
        plan_id="first",
    )
    second: DestructionPlan = plan_destruction(
        request=request,
        analysis=second_fixture.analysis,
        connection=second_fixture.connection,
        now=_NOW.replace(hour=13),
        plan_id="second",
    )

    assert first.manifest_fingerprint == second.manifest_fingerprint
    assert first.plan_fingerprint == second.plan_fingerprint
    assert len(first.manifest_fingerprint) == test_case.expected_fingerprint_length
    assert len(first.plan_fingerprint) == test_case.expected_fingerprint_length


@pytest.mark.parametrize(
    "test_case",
    [
        StableDriftFingerprintTestCase(
            description="part and byte changes do not alter the drift fingerprint",
            changed_stats=(("tbl__orders__deployment_1", 8192, 8),),
            expected_estimated_bytes_changed=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_only_part_bytes_change_when_replanning_then_drift_fingerprint_is_stable(
    test_case: StableDriftFingerprintTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    request: DestructionRequest = DestructionRequest(
        operation="destroy_pipelines",
        target="uat",
        database="analytics",
        metadata_database="metadata",
        pipeline_names=("alpha",),
    )
    first: DestructionPlan = plan_destruction(
        request=request, analysis=fixture.analysis, connection=fixture.connection
    )
    fixture.connection.stats = test_case.changed_stats

    second: DestructionPlan = plan_destruction(
        request=request, analysis=fixture.analysis, connection=fixture.connection
    )

    assert (
        second.estimated_bytes != first.estimated_bytes
    ) is test_case.expected_estimated_bytes_changed
    assert second.plan_fingerprint == first.plan_fingerprint


@pytest.mark.parametrize(
    "test_case",
    [
        DuplicateSelectionTestCase(
            description="a duplicate pipeline selection is rejected",
            expected_error_match="duplicate",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_duplicate_pipeline_when_creating_request_then_selection_is_rejected(
    test_case: DuplicateSelectionTestCase,
) -> None:
    with pytest.raises(ValueError, match=test_case.expected_error_match):
        DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha", "alpha"),
        )


@pytest.mark.parametrize(
    "test_case",
    [
        VirtualHistoryResetTestCase(
            description="reset includes retired virtual history",
            expected_reset_relation_names=("retired_binding", "retired_physical"),
            expected_destroy_excluded_names=("retired_binding", "retired_physical"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_retired_virtual_history_when_planning_then_only_reset_includes_relations(
    test_case: VirtualHistoryResetTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    deployment: AdapterDeploymentRecord = fixture.connection.inventory.deployments[0]
    stale_mapping: AdapterPreparedObjectMapping = AdapterPreparedObjectMapping(
        logical_key=AdapterMetadataObjectKey(
            database=None,
            object_type="table",
            name="retired_binding",
        ),
        physical_name="retired_physical",
        logical_model_name="retired_model",
    )
    fixture.connection.inventory = replace(
        fixture.connection.inventory,
        deployments=(
            replace(
                deployment,
                prepared_object_mappings=(
                    *deployment.prepared_object_mappings,
                    stale_mapping,
                ),
            ),
        ),
        publish_events=(
            *fixture.connection.inventory.publish_events,
            AdapterPublishEventRecord(
                deployment_id=deployment.deployment_id,
                published_at="2026-08-24 11:00:00.000",
                logical_view_names=("retired_binding",),
                bindings=(
                    AdapterStableBinding(
                        database="analytics",
                        logical_name="retired_binding",
                        physical_name="retired_physical",
                    ),
                ),
            ),
        ),
    )
    reset: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="reset_target",
            target="uat",
            database="analytics",
            metadata_database="metadata",
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )
    destroy: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )

    reset_names: frozenset[str] = frozenset(relation.name for relation in reset.relations)
    destroy_names: frozenset[str] = frozenset(relation.name for relation in destroy.relations)
    assert set(test_case.expected_reset_relation_names) <= reset_names
    assert set(test_case.expected_destroy_excluded_names).isdisjoint(destroy_names)


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="recorded virtual history freezes the current generation",
            expected_value="generation:old__orders__deployment_1",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_recorded_virtual_history_when_resetting_then_current_generation_is_frozen(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.catalog = replace(
        fixture.connection.catalog,
        relations=(
            *fixture.connection.catalog.relations,
            CatalogRelation(
                name="old__orders__deployment_1",
                engine="MergeTree",
                columns=(),
                ownership_generation=test_case.expected_value,
            ),
        ),
    )

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="reset_target",
            target="uat",
            database="analytics",
            metadata_database="metadata",
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )

    by_name: dict[str, DestructionRelationEvidence] = {
        relation.name: relation for relation in plan.relations
    }
    assert by_name["old__orders__deployment_1"].catalog_fingerprint == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="recorded noncanonical virtual mapping remains authoritative",
            expected_value="customer_data",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_noncanonical_virtual_mapping_when_resetting_then_recorded_association_is_used(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    deployment: AdapterDeploymentRecord = fixture.connection.inventory.deployments[0]
    mapping: AdapterPreparedObjectMapping = deployment.prepared_object_mappings[0]
    fixture.connection.inventory = replace(
        fixture.connection.inventory,
        deployments=(
            replace(
                deployment,
                prepared_object_mappings=(
                    replace(mapping, physical_name=test_case.expected_value),
                    *deployment.prepared_object_mappings[1:],
                ),
            ),
        ),
    )
    fixture.connection.catalog = replace(
        fixture.connection.catalog,
        relations=(
            *fixture.connection.catalog.relations,
            CatalogRelation(name=test_case.expected_value, engine="MergeTree", columns=()),
        ),
    )

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="reset_target",
            target="uat",
            database="analytics",
            metadata_database="metadata",
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )

    assert test_case.expected_value in {relation.name for relation in plan.relations}


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="committed production classification adds production challenge",
            expected_value="PRODUCTION",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_classified_production_target_when_resetting_then_production_challenge_is_required(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture(target_name="live")
    classified_analysis: CompileAnalysis = cast(
        CompileAnalysis,
        SimpleNamespace(
            realized_project=fixture.analysis.realized_project,
            graph=fixture.analysis.graph,
            compiled_project=SimpleNamespace(
                production_target=True,
                destruction_relation_drop_size_limit=None,
            ),
        ),
    )

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="reset_target",
            target="live",
            database="analytics",
            metadata_database="metadata",
        ),
        analysis=classified_analysis,
        connection=fixture.connection,
    )

    assert plan.challenges[-1] == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseDependencyFailureTestCase(
            description="unmanaged join reads an owned relation after its first source",
            expected_error_value="external_join_view",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unmanaged_join_dependant_when_planning_then_destruction_is_blocked(
    test_case: WarehouseDependencyFailureTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.catalog = replace(
        fixture.connection.catalog,
        relations=(
            *fixture.connection.catalog.relations,
            CatalogRelation(
                name="external_join_view",
                engine="View",
                columns=(),
                source_relation_names=("external_users", "tbl__orders"),
            ),
        ),
    )
    fixture.connection.external_dependants = (test_case.expected_error_value,)

    with pytest.raises(DestructionExternalDependencyError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
            now=_NOW,
        )

    assert raised.value.relation_names == (test_case.expected_error_value,)


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="live DDL drift does not replace manifest association",
            expected_value="generation:tbl__orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_live_ddl_drift_when_planning_then_association_remains_authoritative(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.catalog_matches_resources = False

    plan: DestructionPlan = plan_destruction(
        request=DestructionRequest(
            operation="destroy_pipelines",
            target="uat",
            database="analytics",
            metadata_database="metadata",
            pipeline_names=("alpha",),
        ),
        analysis=fixture.analysis,
        connection=fixture.connection,
    )

    by_name: dict[str, DestructionRelationEvidence] = {
        relation.name: relation for relation in plan.relations
    }
    assert by_name["tbl__orders"].catalog_fingerprint == test_case.expected_value
    assert fixture.connection.catalog_match_count == 0


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="reset without attributable pipeline remains fail closed",
            expected_value="attributable pipeline challenge",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_attributable_pipeline_when_building_challenges_then_reset_is_refused(
    test_case: PlanningBehaviorTestCase,
) -> None:
    with pytest.raises(DestructionSelectionError, match=test_case.expected_value):
        build_destruction_challenges(pipeline_names=())


@pytest.mark.parametrize(
    "test_case",
    [
        PlanningBehaviorTestCase(
            description="recorded pipeline identity constrains current manifest name",
            expected_value="tbl__orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unmanaged_dependant_when_destroying_associated_relation_then_it_blocks(
    test_case: PlanningBehaviorTestCase,
) -> None:
    fixture: PlanningFixture = build_planning_fixture()
    fixture.connection.external_dependants = (test_case.expected_value,)

    with pytest.raises(DestructionExternalDependencyError) as raised:
        plan_destruction(
            request=DestructionRequest(
                operation="destroy_pipelines",
                target="uat",
                database="analytics",
                metadata_database="metadata",
                pipeline_names=("alpha",),
            ),
            analysis=fixture.analysis,
            connection=fixture.connection,
        )

    assert raised.value.relation_names == (test_case.expected_value,)


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
