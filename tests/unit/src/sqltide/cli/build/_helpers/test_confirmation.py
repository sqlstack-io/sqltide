from collections.abc import Iterator

import pytest

from sqltide._native import confirm_build
from sqltide.cli.build.models import BuildProtectionRequirement
from tests.unit.src.sqltide.cli.build._helpers._test_types import (
    BuildConfirmationTestCase,
    MultipleBuildProtectionTestCase,
)
from tests.unit.src.sqltide.cli.build._helpers.helpers import (
    build_confirmation_options,
    build_protection_requirement,
)


@pytest.mark.parametrize(
    "test_case",
    [
        BuildConfirmationTestCase(
            description="unprotected auto-approved build proceeds",
            protection_requirements=(),
            auto_approve=True,
            confirmations=(),
            input_response=None,
            expected_confirmed=True,
            expected_stderr_fragment="",
        ),
        BuildConfirmationTestCase(
            description="protected auto-approved build rejects a missing word",
            protection_requirements=build_protection_requirement(),
            auto_approve=True,
            confirmations=(),
            input_response=None,
            expected_confirmed=False,
            expected_stderr_fragment="PROTECTED PIPELINE: protected_prices",
        ),
        BuildConfirmationTestCase(
            description="protected auto-approved build accepts the exact word",
            protection_requirements=build_protection_requirement(),
            auto_approve=True,
            confirmations=("DEPLOY_PROTECTED_PRICES",),
            input_response=None,
            expected_confirmed=True,
            expected_stderr_fragment="Interrupts protected trading prices.",
        ),
        BuildConfirmationTestCase(
            description="interactive protected build accepts a typed exact word",
            protection_requirements=build_protection_requirement(),
            auto_approve=False,
            confirmations=(),
            input_response="DEPLOY_PROTECTED_PRICES",
            expected_confirmed=True,
            expected_stderr_fragment="Interrupts protected trading prices.",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_build_protection_when_confirming_then_applies_required_gate(
    test_case: BuildConfirmationTestCase,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: test_case.input_response or "")

    confirmed: bool = confirm_build(
        options=build_confirmation_options(
            auto_approve=test_case.auto_approve,
            confirmations=test_case.confirmations,
        ),
        plan_text="plan",
        protection_requirements=test_case.protection_requirements,
    )

    assert confirmed is test_case.expected_confirmed
    assert test_case.expected_stderr_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        MultipleBuildProtectionTestCase(
            description="prompts once for each protected pipeline",
            responses=("DEPLOY_PROTECTED_PRICES", "DEPLOY_SETTLEMENT"),
            expected_pipeline_names=("protected_prices", "settlement"),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_protected_pipelines_when_confirming_interactively_then_prompts_for_each(
    test_case: MultipleBuildProtectionTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    responses: Iterator[str] = iter(test_case.responses)

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", answer)

    confirmed: bool = confirm_build(
        options=build_confirmation_options(auto_approve=False, confirmations=()),
        plan_text="plan",
        protection_requirements=(
            *build_protection_requirement(),
            BuildProtectionRequirement(
                pipeline_name="settlement",
                warning="Interrupts settlement processing.",
                confirmation="DEPLOY_SETTLEMENT",
            ),
        ),
    )

    assert confirmed is True
    assert len(prompts) == len(test_case.expected_pipeline_names)
    assert all(
        name in prompt
        for name, prompt in zip(test_case.expected_pipeline_names, prompts, strict=True)
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
