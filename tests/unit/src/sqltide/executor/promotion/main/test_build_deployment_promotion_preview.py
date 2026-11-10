import pytest

from sqltide._native import build_binding_replacement_preview
from sqltide.adapter.models import (
    AdapterBindingReplacementRequest,
    AdapterStableBinding,
    AdapterStableBindingRemoval,
    InspectedActiveTableBinding,
)
from sqltide.executor.promotion.models import (
    DeploymentPromotionPreview,
    PromotionBindingAddition,
    PromotionBindingRemoval,
    PromotionBindingReplacement,
    PromotionOrphanedRelation,
)
from sqltide.executor.promotion.types import PromotionPreviewClassification
from tests.unit.src.sqltide.executor.promotion.main._test_types import (
    PromotionPreviewTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        PromotionPreviewTestCase(
            description="additions without live binding changes are an initial publish",
            binding_request=AdapterBindingReplacementRequest(
                bindings=(
                    AdapterStableBinding(
                        database="analytics",
                        logical_name="orders",
                        physical_name="orders__new",
                    ),
                )
            ),
            active_bindings=(),
            expected_preview=DeploymentPromotionPreview(
                classification=PromotionPreviewClassification.INITIAL_PUBLISH,
                additions=(
                    PromotionBindingAddition(
                        database="analytics",
                        logical_name="orders",
                        physical_name="orders__new",
                    ),
                ),
                replacements=(),
                removals=(),
                orphaned_relations=(),
            ),
        ),
        PromotionPreviewTestCase(
            description="candidate additions with an obsolete removal are a promotion",
            binding_request=AdapterBindingReplacementRequest(
                bindings=(
                    AdapterStableBinding(
                        database="analytics",
                        logical_name="orders_current",
                        physical_name="orders_current__new",
                    ),
                ),
                removals=(
                    AdapterStableBindingRemoval(database="analytics", logical_name="orders_legacy"),
                ),
            ),
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="orders_legacy",
                    physical_name="orders_legacy__old",
                ),
            ),
            expected_preview=DeploymentPromotionPreview(
                classification=PromotionPreviewClassification.PROMOTION,
                additions=(
                    PromotionBindingAddition(
                        database="analytics",
                        logical_name="orders_current",
                        physical_name="orders_current__new",
                    ),
                ),
                replacements=(),
                removals=(
                    PromotionBindingRemoval(
                        database="analytics",
                        logical_name="orders_legacy",
                        physical_name="orders_legacy__old",
                    ),
                ),
                orphaned_relations=(
                    PromotionOrphanedRelation(
                        database="analytics", physical_name="orders_legacy__old"
                    ),
                ),
            ),
        ),
        PromotionPreviewTestCase(
            description="replacement only orphans a physical relation with no remaining binding",
            binding_request=AdapterBindingReplacementRequest(
                bindings=(
                    AdapterStableBinding(
                        database="analytics",
                        logical_name="orders",
                        physical_name="orders__new",
                    ),
                )
            ),
            active_bindings=(
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="orders",
                    physical_name="orders__old",
                ),
                InspectedActiveTableBinding(
                    database="analytics",
                    logical_name="orders_alias",
                    physical_name="orders__old",
                ),
            ),
            expected_preview=DeploymentPromotionPreview(
                classification=PromotionPreviewClassification.PROMOTION,
                additions=(),
                replacements=(
                    PromotionBindingReplacement(
                        database="analytics",
                        logical_name="orders",
                        from_physical_name="orders__old",
                        to_physical_name="orders__new",
                    ),
                ),
                removals=(),
                orphaned_relations=(),
            ),
        ),
        PromotionPreviewTestCase(
            description="removal without an active binding is a no-op",
            binding_request=AdapterBindingReplacementRequest(
                bindings=(),
                removals=(
                    AdapterStableBindingRemoval(
                        database="analytics", logical_name="orders_missing"
                    ),
                ),
            ),
            active_bindings=(),
            expected_preview=DeploymentPromotionPreview(
                classification=PromotionPreviewClassification.PROMOTION,
                additions=(),
                replacements=(),
                removals=(),
                orphaned_relations=(),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_binding_request_when_previewing_promotion_then_effects_are_exact(
    test_case: PromotionPreviewTestCase,
) -> None:
    preview: DeploymentPromotionPreview = build_binding_replacement_preview(
        binding_request=test_case.binding_request,
        active_bindings=test_case.active_bindings,
    )

    assert preview == test_case.expected_preview


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
