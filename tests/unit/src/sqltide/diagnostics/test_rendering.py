import json
from pathlib import Path

import pytest

from sqltide.adapter.exceptions import AdapterConfigurationError
from sqltide.diagnostics.main._render_diagnostic_json import render_diagnostic_json
from sqltide.diagnostics.main.attach_error_diagnostic import attach_error_diagnostic
from sqltide.diagnostics.main.render_diagnostic import render_diagnostic
from sqltide.diagnostics.main.render_error import render_error
from sqltide.diagnostics.models import CompilerDiagnostic
from sqltide.diagnostics.types import DiagnosticPhase
from tests.unit.src.sqltide.diagnostics._test_types import (
    DiagnosticJsonTestCase,
    DiagnosticTextTestCase,
    RuntimeDiagnosticTestCase,
)
from tests.unit.src.sqltide.diagnostics.helpers import build_diagnostic


@pytest.mark.parametrize(
    "test_case",
    [
        DiagnosticTextTestCase(
            description="renders primary and related source spans with help",
            expected_fragments=(
                "error [SQT-COMPILE-TEST] projection has no alias",
                "phase: compilation",
                "resource: orders",
                "--> models/orders.sql:2:8",
                "  2 | SELECT amount + tax",
                "^^^^^",
                "declared here: --> models/orders.sql:1:1",
                "help: alias every projected expression",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_structured_diagnostic_when_rendering_text_then_includes_source_context(
    test_case: DiagnosticTextTestCase,
) -> None:
    diagnostic: CompilerDiagnostic = build_diagnostic()

    rendered: str = render_diagnostic(
        diagnostic=diagnostic,
        source_by_path={Path("models/orders.sql"): "MODEL orders\nSELECT amount + tax\n"},
    )

    assert tuple(fragment in rendered for fragment in test_case.expected_fragments) == (
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DiagnosticJsonTestCase(
            description="serializes complete source spans deterministically",
            expected_phase="compilation",
            expected_code="SQT-COMPILE-TEST",
            expected_location=("models/orders.sql", 2, 8, 2, 12),
            expected_related_label="declared here",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_structured_diagnostic_when_rendering_json_then_preserves_full_span(
    test_case: DiagnosticJsonTestCase,
) -> None:
    rendered: str = render_diagnostic_json(diagnostic=build_diagnostic())
    payload: dict[str, object] = json.loads(rendered)
    location: dict[str, object] = payload["location"]
    related: dict[str, object] = payload["related_locations"][0]

    assert payload["phase"] == test_case.expected_phase
    assert payload["code"] == test_case.expected_code
    assert (
        location["path"],
        location["line"],
        location["column"],
        location["end_line"],
        location["end_column"],
    ) == test_case.expected_location
    assert related["label"] == test_case.expected_related_label


@pytest.mark.parametrize(
    "test_case",
    [
        RuntimeDiagnosticTestCase(
            description="attaches runtime phase context to a neutral adapter error",
            error_message="connection configuration is invalid",
            expected_fragments=(
                "error [SQT-RUNTIME-001] connection configuration is invalid",
                "phase: runtime",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adapter_error_when_attaching_runtime_context_then_renders_structured_diagnostic(
    test_case: RuntimeDiagnosticTestCase,
) -> None:
    error: AdapterConfigurationError = AdapterConfigurationError(test_case.error_message)
    _ = attach_error_diagnostic(
        error=error,
        phase=DiagnosticPhase.RUNTIME,
        code="SQT-RUNTIME-001",
    )

    rendered: str = render_error(error)

    assert tuple(fragment in rendered for fragment in test_case.expected_fragments) == (
        True,
        True,
    )
