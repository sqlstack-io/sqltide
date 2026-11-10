from pathlib import Path

import pytest

from sqltide._native import drifted_managed_sources
from sqltide.adapter.models import CatalogSnapshot
from sqltide.compiler.planner.models import DirectWarehouseSnapshot
from sqltide.executor.direct.models import DirectBuildRequest, DriftedManagedSource
from tests.unit.src.sqltide.executor.direct.main._test_types import (
    DirectDriftedSourceOwnerTestCase,
    DirectDriftedSourceShapeTestCase,
)
from tests.unit.src.sqltide.executor.direct.main.helpers import (
    build_direct_execution_snapshot,
    build_managed_source_execution_request,
    build_managed_source_snapshot,
    without_models,
)

_TOPIC_DRIFT: tuple[tuple[str, str], ...] = (("kafka_topic_list", "source.orders.previous"),)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectDriftedSourceOwnerTestCase(
            description="a declared setting absent from the deployed source names its reader",
            declared_settings=(("kafka_max_block_size", "500"),),
            catalog_setting_overrides=(),
            omitted_catalog_settings=("kafka_max_block_size",),
            extra_landing_columns=(),
            expected_drifted_sources=(("orders", ("pl__orders",), ("kafka_max_block_size",)),),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="a changed topic names only the direct reader, not a downstream pipeline",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            omitted_catalog_settings=(),
            extra_landing_columns=(),
            expected_drifted_sources=(("orders", ("pl__orders",), ("kafka_topic_list",)),),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="several direct readers are named in sorted order",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            omitted_catalog_settings=(),
            extra_landing_columns=(),
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            expected_drifted_sources=(
                ("orders", ("pl__order_audit", "pl__orders"), ("kafka_topic_list",)),
            ),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="a deployed source matching its declaration names no pipeline",
            declared_settings=(("kafka_max_block_size", "500"),),
            catalog_setting_overrides=(),
            omitted_catalog_settings=(),
            extra_landing_columns=(),
            expected_drifted_sources=(),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="a masked deployed setting is not drift",
            declared_settings=(),
            catalog_setting_overrides=(("kafka_broker_list", "[HIDDEN]"),),
            omitted_catalog_settings=(),
            extra_landing_columns=(),
            expected_drifted_sources=(),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="a different consumer group alone is not settings drift",
            declared_settings=(),
            catalog_setting_overrides=(("kafka_group_name", "sqltide_orders_previous"),),
            omitted_catalog_settings=(),
            extra_landing_columns=(),
            expected_drifted_sources=(),
        ),
        DirectDriftedSourceOwnerTestCase(
            description="landing column drift alone is not settings drift",
            declared_settings=(),
            catalog_setting_overrides=(),
            omitted_catalog_settings=(),
            extra_landing_columns=(("legacy_note", "String"),),
            expected_drifted_sources=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deployed_managed_source_when_naming_drifted_owners_then_only_readers_are_named(
    test_case: DirectDriftedSourceOwnerTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_managed_source_execution_request(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        extra_reader_pipelines=test_case.extra_reader_pipelines,
    )
    snapshot: DirectWarehouseSnapshot = build_managed_source_snapshot(
        request=request,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        extra_landing_columns=test_case.extra_landing_columns,
    )

    drifted: tuple[DriftedManagedSource, ...] = drifted_managed_sources(
        realized_project=request.realized_project,
        catalog=snapshot.catalog,
    )

    assert tuple(
        (source.source_key.name, source.owner_pipeline_names, source.drifted_setting_names)
        for source in drifted
    ) == (test_case.expected_drifted_sources)


@pytest.mark.parametrize(
    "test_case",
    [
        DirectDriftedSourceShapeTestCase(
            description="an undeployed source is not drift",
            removed_model_names=frozenset(),
            deployed=False,
            expected_drifted_sources=(),
        ),
        DirectDriftedSourceShapeTestCase(
            description="a source no pipeline reads directly is owned by every declaring pipeline",
            removed_model_names=frozenset({"alpha"}),
            deployed=True,
            expected_drifted_sources=(
                ("orders", ("pl__order_summary", "pl__orders"), ("kafka_topic_list",)),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_source_shape_when_naming_drifted_owners_then_ownership_rule_applies(
    test_case: DirectDriftedSourceShapeTestCase,
    tmp_path: Path,
) -> None:
    request: DirectBuildRequest = build_managed_source_execution_request(
        project_root=tmp_path,
        declared_settings=(),
    )
    catalog: CatalogSnapshot = {
        True: build_managed_source_snapshot(
            request=request, catalog_setting_overrides=_TOPIC_DRIFT
        ).catalog,
        False: build_direct_execution_snapshot().catalog,
    }[test_case.deployed]

    drifted: tuple[DriftedManagedSource, ...] = drifted_managed_sources(
        realized_project=without_models(
            request=request, model_names=test_case.removed_model_names
        ).realized_project,
        catalog=catalog,
    )

    assert tuple(
        (source.source_key.name, source.owner_pipeline_names, source.drifted_setting_names)
        for source in drifted
    ) == (test_case.expected_drifted_sources)
