from pathlib import Path

import pytest

from scripts.shape_surface._helpers.surface import surface_errors
from tests.unit.scripts.shape_surface.main._test_types import ShapeSurfaceTestCase
from tests.unit.scripts.shape_surface.main.helpers import load_fixture, record_fixture

SHAPES: str = (
    "import enum\n"
    "from dataclasses import dataclass, field\n"
    "\n"
    "\n"
    "class Mode(enum.StrEnum):\n"
    "    DIRECT = 'direct'\n"
    "    VIRTUAL = 'virtual'\n"
    "\n"
    "\n"
    "@dataclass(frozen=True)\n"
    "class Key:\n"
    "    database: str\n"
    "    name: str\n"
    "    note: str = field(default='', compare=False)\n"
    "\n"
    "\n"
    "class PlanError(ValueError):\n"
    "    def __init__(self, pipeline: str) -> None:\n"
    "        super().__init__(f'Pipeline {pipeline!r} cannot be planned')\n"
)


@pytest.mark.parametrize(
    "test_case",
    [
        ShapeSurfaceTestCase(
            description="an unchanged surface passes",
            recorded_source=SHAPES,
            current_source=SHAPES,
            expected_messages=(),
        ),
        ShapeSurfaceTestCase(
            description="a field that stops taking part in equality fails",
            recorded_source=SHAPES,
            current_source=SHAPES.replace(
                "    name: str\n", "    name: str = field(compare=False)\n"
            ),
            expected_messages=("shape_surface_fixture:Key fields changed",),
        ),
        ShapeSurfaceTestCase(
            description="a renamed enum value fails",
            recorded_source=SHAPES,
            current_source=SHAPES.replace("'virtual'", "'virtual_environment'"),
            expected_messages=("shape_surface_fixture:Mode members changed",),
        ),
        ShapeSurfaceTestCase(
            description="an exception that changes its message fails",
            recorded_source=SHAPES,
            current_source=SHAPES.replace("cannot be planned", "is not plannable"),
            expected_messages=("shape_surface_fixture:PlanError instance changed",),
        ),
        ShapeSurfaceTestCase(
            description="an exception that changes its base fails",
            recorded_source=SHAPES,
            current_source=SHAPES.replace(
                "class PlanError(ValueError)", "class PlanError(Exception)"
            ),
            expected_messages=("shape_surface_fixture:PlanError mro changed",),
        ),
        ShapeSurfaceTestCase(
            description="a dropped name fails unless REMOVED gives the reason",
            recorded_source=SHAPES,
            current_source=SHAPES.replace("class Mode(", "class _Mode("),
            expected_messages=("shape_surface_fixture:Mode is no longer importable",),
        ),
        ShapeSurfaceTestCase(
            description="a dropped name listed in REMOVED passes",
            recorded_source=SHAPES,
            current_source=SHAPES.replace("class Mode(", "class _Mode("),
            removed={"shape_surface_fixture:Mode": "Unused."},
            expected_messages=(),
        ),
        ShapeSurfaceTestCase(
            description="a REMOVED entry for a name that still exists fails",
            recorded_source=SHAPES,
            current_source=SHAPES,
            removed={"shape_surface_fixture:Mode": "Unused."},
            expected_messages=("shape_surface_fixture:Mode is listed in REMOVED but still exists",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_recorded_surface_when_checking_then_regressions_are_reported(
    test_case: ShapeSurfaceTestCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("sys.dont_write_bytecode", True)
    baseline: dict[str, dict[str, object]] = record_fixture(
        root=tmp_path, source=test_case.recorded_source
    )
    load_fixture(root=tmp_path, source=test_case.current_source)

    errors: list[str] = surface_errors(
        baseline=baseline, removed=test_case.removed, volatile=frozenset()
    )

    assert len(errors) == len(test_case.expected_messages), errors
    for error, expected in zip(errors, test_case.expected_messages, strict=True):
        assert error.startswith(expected), error
