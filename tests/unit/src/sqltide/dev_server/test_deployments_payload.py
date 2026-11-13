from typing import cast

import pytest

from sqltide._native import (
    build_deployment_detail_payload,
    build_deployments_payload,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    DeploymentDetailMissingTestCase,
    DeploymentDetailTestCase,
    DeploymentInitialPublishSafetyTestCase,
    DeploymentPartialPromotionTestCase,
    DeploymentsPayloadTestCase,
    DeploymentStaleUpstreamTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    DEPLOYMENT_ACTIVE_ID,
    DEPLOYMENT_STAGED_ID,
    DEPLOYMENT_SUPERSEDED_ID,
    build_fake_candidate_only_promotion_connection,
    build_fake_deployment_connection,
    build_fake_partial_promotion_connection,
    build_fake_stale_upstream_connection,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentsPayloadTestCase(
            description="newest deployment first with landing views excluded from model counts",
            expected_deployment_ids=(DEPLOYMENT_STAGED_ID, DEPLOYMENT_ACTIVE_ID),
            expected_states=("staged", "active"),
            expected_model_counts=(3, 2),
            expected_rows=(1267, 1050),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_staged_and_active_deployments_when_building_payload_then_totals_are_exact(
    test_case: DeploymentsPayloadTestCase,
) -> None:
    payload: dict[str, object] = build_deployments_payload(
        connection=build_fake_deployment_connection(),
        database="analytics",
        metadata_database="analytics",
    )

    deployments: list[dict[str, object]] = cast(list[dict[str, object]], payload["deployments"])
    assert tuple(str(item["deploymentId"]) for item in deployments) == (
        test_case.expected_deployment_ids
    )
    assert tuple(str(item["state"]) for item in deployments) == test_case.expected_states
    assert tuple(int(cast(int, item["modelCount"])) for item in deployments) == (
        test_case.expected_model_counts
    )
    assert tuple(int(cast(int, item["rows"])) for item in deployments) == test_case.expected_rows


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDetailTestCase(
            description="staged detail pairs each model with its live counterpart",
            deployment_id=DEPLOYMENT_STAGED_ID,
            expected_state="staged",
            expected_logical_names=("tbl__orders", "tbl__refunds", "tbl__revenue"),
            expected_staged_rows=(1200, 7, 60),
            expected_live_rows=(1000, None, 50),
            expected_new_flags=(False, True, False),
            expected_orphan_relations=2,
            expected_preview_classification="promotion",
            expected_additions=("tbl__refunds",),
            expected_replacements=("tbl__orders", "tbl__revenue"),
            expected_removals=(),
        ),
        DeploymentDetailTestCase(
            description="active detail reports no orphaned relations",
            deployment_id=DEPLOYMENT_ACTIVE_ID,
            expected_state="active",
            expected_logical_names=("tbl__orders", "tbl__revenue"),
            expected_staged_rows=(1000, 50),
            expected_live_rows=(1000, 50),
            expected_new_flags=(False, False),
            expected_orphan_relations=0,
            expected_preview_classification=None,
            expected_additions=(),
            expected_replacements=(),
            expected_removals=(),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_when_building_detail_then_live_comparison_is_exact(
    test_case: DeploymentDetailTestCase,
) -> None:
    payload: dict[str, object] | None = build_deployment_detail_payload(
        connection=build_fake_deployment_connection(),
        database="analytics",
        metadata_database="analytics",
        deployment_id=test_case.deployment_id,
    )

    resolved: dict[str, object] = cast(dict[str, object], payload)
    assert str(resolved["state"]) == test_case.expected_state
    models: list[dict[str, object]] = cast(list[dict[str, object]], resolved["models"])
    assert tuple(str(model["logicalName"]) for model in models) == (
        test_case.expected_logical_names
    )
    assert tuple(int(cast(int, model["stagedRows"])) for model in models) == (
        test_case.expected_staged_rows
    )
    assert tuple(cast(int | None, model["liveRows"]) for model in models) == (
        test_case.expected_live_rows
    )
    assert tuple(bool(model["isNew"]) for model in models) == test_case.expected_new_flags
    orphan: dict[str, object] = cast(dict[str, object], resolved["wouldOrphan"])
    assert int(cast(int, orphan["relationCount"])) == test_case.expected_orphan_relations
    preview: dict[str, object] = cast(dict[str, object], resolved["promotionPreview"] or {})
    assert preview.get("classification") == test_case.expected_preview_classification
    for field, expected_names in (
        ("additions", test_case.expected_additions),
        ("replacements", test_case.expected_replacements),
        ("removals", test_case.expected_removals),
    ):
        entries: list[dict[str, object]] = cast(list[dict[str, object]], preview.get(field, []))
        assert tuple(str(entry["logicalName"]) for entry in entries) == expected_names


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentInitialPublishSafetyTestCase(
            description="candidate-only deployment with obsolete live binding remains a promotion",
            expected_candidate_new_flags=(True,),
            expected_classification="promotion",
            expected_removed_logical_names=("tbl__orders_legacy",),
            expected_orphan_relation_names=(f"tbl__orders_legacy__{DEPLOYMENT_ACTIVE_ID}",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_candidate_only_deployment_when_previewing_then_obsolete_removal_is_exposed(
    test_case: DeploymentInitialPublishSafetyTestCase,
) -> None:
    payload: dict[str, object] | None = build_deployment_detail_payload(
        connection=build_fake_candidate_only_promotion_connection(),
        database="analytics",
        metadata_database="analytics",
        deployment_id=DEPLOYMENT_STAGED_ID,
    )

    resolved: dict[str, object] = cast(dict[str, object], payload)
    models: list[dict[str, object]] = cast(list[dict[str, object]], resolved["models"])
    assert tuple(bool(model["isNew"]) for model in models) == (
        test_case.expected_candidate_new_flags
    )
    preview: dict[str, object] = cast(dict[str, object], resolved["promotionPreview"])
    assert str(preview["classification"]) == test_case.expected_classification
    removals: list[dict[str, object]] = cast(list[dict[str, object]], preview["removals"])
    assert tuple(str(removal["logicalName"]) for removal in removals) == (
        test_case.expected_removed_logical_names
    )
    orphan: dict[str, object] = cast(dict[str, object], resolved["wouldOrphan"])
    assert tuple(cast(list[str], orphan["relationNames"])) == (
        test_case.expected_orphan_relation_names
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentPartialPromotionTestCase(
            description="partially switched deployment retains its remaining promotion actions",
            expected_state="active",
            expected_additions=("tbl__refunds",),
            expected_replacements=("tbl__revenue",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_partial_promotion_when_building_detail_then_remaining_actions_are_retryable(
    test_case: DeploymentPartialPromotionTestCase,
) -> None:
    payload: dict[str, object] | None = build_deployment_detail_payload(
        connection=build_fake_partial_promotion_connection(),
        database="analytics",
        metadata_database="analytics",
        deployment_id=DEPLOYMENT_STAGED_ID,
    )

    resolved: dict[str, object] = cast(dict[str, object], payload)
    preview: dict[str, object] = cast(dict[str, object], resolved["promotionPreview"])
    additions: list[dict[str, object]] = cast(list[dict[str, object]], preview["additions"])
    replacements: list[dict[str, object]] = cast(list[dict[str, object]], preview["replacements"])
    assert resolved["state"] == test_case.expected_state
    assert tuple(str(item["logicalName"]) for item in additions) == test_case.expected_additions
    assert tuple(str(item["logicalName"]) for item in replacements) == (
        test_case.expected_replacements
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentDetailMissingTestCase(
            description="unknown deployment id yields no payload",
            deployment_id="20260101T000000Z_missing",
            expected_payload=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unknown_deployment_when_building_detail_then_no_payload_is_returned(
    test_case: DeploymentDetailMissingTestCase,
) -> None:
    payload: dict[str, object] | None = build_deployment_detail_payload(
        connection=build_fake_deployment_connection(),
        database="analytics",
        metadata_database="analytics",
        deployment_id=test_case.deployment_id,
    )

    assert payload == test_case.expected_payload


@pytest.mark.parametrize(
    "test_case",
    [
        DeploymentStaleUpstreamTestCase(
            description="downstream reading the published upstream stays promotable",
            upstream_deployment_id=DEPLOYMENT_ACTIVE_ID,
            expected_blocked_reason=None,
        ),
        DeploymentStaleUpstreamTestCase(
            description="downstream reading a superseded upstream shows the promote refusal",
            upstream_deployment_id=DEPLOYMENT_SUPERSEDED_ID,
            expected_blocked_reason=(
                f"Deployment {DEPLOYMENT_STAGED_ID} reads an upstream version that is no longer "
                "published, so it cannot be promoted:\n"
                f"- tbl__revenue reads tbl__orders__{DEPLOYMENT_SUPERSEDED_ID}, but tbl__orders "
                f"now reads tbl__orders__{DEPLOYMENT_ACTIVE_ID}\n"
                "Rebuild the candidate with `sqt build --select tbl__revenue`, then promote that "
                "build by the ID it prints."
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_staged_downstream_when_previewing_then_stale_upstream_refusal_is_shown(
    test_case: DeploymentStaleUpstreamTestCase,
) -> None:
    payload: dict[str, object] | None = build_deployment_detail_payload(
        connection=build_fake_stale_upstream_connection(
            upstream_deployment_id=test_case.upstream_deployment_id
        ),
        database="analytics",
        metadata_database="analytics",
        deployment_id=DEPLOYMENT_STAGED_ID,
    )

    resolved: dict[str, object] = cast(dict[str, object], payload)
    preview: dict[str, object] = cast(dict[str, object], resolved["promotionPreview"])
    assert preview["blockedReason"] == test_case.expected_blocked_reason


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
