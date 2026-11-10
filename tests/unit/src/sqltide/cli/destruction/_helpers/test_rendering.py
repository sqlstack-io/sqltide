import builtins
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from sqltide._native import read_destruction_challenges, render_destruction_plan
from sqltide.executor.destruction.models import DestructionPlan
from tests.unit.src.sqltide.cli.destruction._helpers._test_types import (
    DestructionChallengeInputTestCase,
    DestructionRenderingTestCase,
)
from tests.unit.src.sqltide.cli.destruction.helpers import destruction_plan


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionRenderingTestCase(
            description="renders frozen scope evidence relations preservation and challenges",
            expected_fragments=(
                "Plan ID: destruction_reviewed",
                "Operation: destroy_pipelines",
                "Target: uat",
                "Database: analytics",
                "Created at: 2026-08-24T12:00:00+00:00",
                "Manifest fingerprint: manifest-sha",
                "Plan fingerprint: plan-sha",
                "Requested pipelines: alpha",
                "Affected models: orders",
                "Preserves managed sources: yes",
                "Estimated active-part bytes: 2048",
                "Effective relation DROP limit: 107374182400 bytes",
                "ClickHouse server default DROP limit: 50000000000 bytes",
                "SQLTide destruction DROP override: 107374182400 bytes",
                "Irreversible: yes",
                "Recreation: authored definitions remain",
                "- analytics.tbl__orders",
                "  Kind: table",
                "  Active parts: 2",
                "  Catalog fingerprint: catalog-sha",
                "  Ownership: current_manifest",
                "  Dependencies: none",
                "Required typed challenges:\n- alpha",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_frozen_plan_when_rendering_then_complete_evidence_is_printed(
    test_case: DestructionRenderingTestCase,
) -> None:
    # Given: A frozen plan containing warehouse evidence.
    plan: DestructionPlan = replace(
        destruction_plan(),
        relation_drop_size_limit=107_374_182_400,
        relation_drop_size_server_limit=50_000_000_000,
        relation_drop_size_override=107_374_182_400,
    )

    # When: The operator-facing plan is rendered.
    rendered: str = render_destruction_plan(plan)

    # Then: Every safety-relevant section is present before review.
    for fragment in test_case.expected_fragments:
        assert fragment in rendered


@pytest.mark.parametrize(
    "test_case",
    [
        DestructionChallengeInputTestCase(
            description="preserves surrounding challenge whitespace",
            response=" alpha ",
            expected_responses=(" alpha ",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_typed_challenge_when_reading_then_response_is_not_stripped(
    test_case: DestructionChallengeInputTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Input containing whitespace around the challenge.
    input_mock: MagicMock = MagicMock(return_value=test_case.response)
    monkeypatch.setattr(builtins, "input", input_mock)

    # When: The challenge is read.
    responses: tuple[str, ...] = read_destruction_challenges(destruction_plan())

    # Then: The exact entered value is retained for store validation.
    assert responses == test_case.expected_responses
