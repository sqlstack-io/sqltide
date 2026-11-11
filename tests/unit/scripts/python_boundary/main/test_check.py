from pathlib import Path

import pytest

from scripts.python_boundary.main.check import run_python_boundary_check
from tests.unit.scripts.python_boundary.main._test_types import PythonBoundaryTestCase
from tests.unit.scripts.python_boundary.main.helpers import write_boundary_project

SWALLOWED: str = (
    "from sqltide import _native\n"
    "\n"
    "\n"
    "def digest(text):\n"
    "    try:\n"
    "        return _native.content_digest([text])\n"
    "    except ValueError:\n"
    "        return None\n"
)
RERAISED: str = (
    "from sqltide._native import content_digest\n"
    "\n"
    "\n"
    "def digest(text):\n"
    "    try:\n"
    "        return content_digest([text])\n"
    "    except ValueError as error:\n"
    "        raise RuntimeError(text) from error\n"
)
CONDITIONAL: str = (
    "from sqltide._native import content_digest\n"
    "\n"
    "\n"
    "def digest(text, strict):\n"
    "    try:\n"
    "        return content_digest([text])\n"
    "    except ValueError:\n"
    "        if not strict:\n"
    "            return None\n"
    "        raise\n"
)
SUPPRESSED: str = (
    "import contextlib\n"
    "\n"
    "from sqltide.compiler.graph.main._native_project_graph import native_project_graph\n"
    "\n"
    "\n"
    "class Graph:\n"
    "    def build(self, project):\n"
    "        with contextlib.suppress(ValueError):\n"
    "            return native_project_graph(project)\n"
    "        return None\n"
)
REEXPORT: str = (
    '"""Compile models, defined natively."""\n'
    "\n"
    "from sqltide._native import Column as Column\n"
    "from sqltide._native import ObjectKey as ObjectKey\n"
    "\n"
    '__all__ = ["Column", "ObjectKey"]\n'
)
EXTRA_ASSIGNMENT: str = REEXPORT + 'DEFAULT_NAME: str = "orders"\n'
EXTRA_FUNCTION: str = REEXPORT + "\n\ndef build():\n    return Column\n"
NATIVE_COLUMN: str = "from sqltide._native import Column as Column\n"
FOREIGN_IMPORT: str = REEXPORT.replace(
    NATIVE_COLUMN, "from sqltide.compiler.columns import Column\n"
)
CONDITIONAL_IMPORT: str = REEXPORT.replace(NATIVE_COLUMN, "if True:\n    " + NATIVE_COLUMN)
GUARDED_IMPORT: str = REEXPORT.replace(
    NATIVE_COLUMN, "try:\n    " + NATIVE_COLUMN + "except ImportError:\n    Column = None\n"
)
RENAMED_IMPORT: str = REEXPORT.replace("Column as Column", "Column as ColumnSpec")
EXTRA_EXPORT: str = REEXPORT.replace('"Column", "ObjectKey"', '"Column", "ObjectKey", "Table"')
DISCOVERY_TYPES: str = (
    '"""Discovery types, defined natively."""\n'
    "\n"
    "from sqltide._native import DiscoveryRefType as RefType\n"
    "from sqltide._native import PipelineMode as PipelineMode\n"
)
UNLISTED_ALIAS: str = DISCOVERY_TYPES.replace(
    "PipelineMode as PipelineMode", "DiscoveryPipelineMode as PipelineMode"
)
WRONG_ALIAS: str = DISCOVERY_TYPES.replace("DiscoveryRefType as RefType", "DiscoveryRefType as Ref")
STAYS: str = '[[stays]]\npath = "executor"\nrule = 1\nnote = "Rule 1: runs plans."\n'
REVIEWED: str = (
    '[[handled_native_errors]]\nlocation = "compiler/keys.py::digest"\nnote = "No key."\n'
)


@pytest.mark.parametrize(
    "test_case",
    [
        PythonBoundaryTestCase(
            description="pure native re-export modules are reported but not counted",
            source_files={"compiler/models.py": REEXPORT, "compiler/plan.py": "a = 1\n"},
            policy="remaining_lines = 1\n",
            expected_messages=(
                "1 movable Python lines remain; 1 pure native re-export files (6 lines)",
            ),
        ),
        PythonBoundaryTestCase(
            description="a listed native alias in its own module is a pure re-export",
            source_files={"compiler/discovery/types.py": DISCOVERY_TYPES},
            policy="remaining_lines = 0\n",
            expected_messages=("1 pure native re-export files (4 lines)",),
        ),
        PythonBoundaryTestCase(
            description="staying modules are not counted",
            source_files={"compiler/plan.py": "a = 1\nb = 2\n", "executor/run.py": "c = 3\n"},
            policy="remaining_lines = 2\n" + STAYS,
            expected_messages=("2 movable Python lines remain",),
        ),
        PythonBoundaryTestCase(
            description="names that only contain the letters native are not native calls",
            source_files={
                "compiler/plan.py": (
                    "from sqltide.compiler.alternatives import alternative_plan\n"
                    "\n"
                    "\n"
                    "def plan():\n"
                    "    try:\n"
                    "        return alternative_plan()\n"
                    "    except ValueError:\n"
                    "        return None\n"
                )
            },
            policy="remaining_lines = 8\n",
            expected_messages=("8 movable Python lines remain",),
        ),
        PythonBoundaryTestCase(
            description="re-raised native failures are allowed",
            source_files={"compiler/keys.py": RERAISED},
            policy="remaining_lines = 8\n",
            expected_messages=("8 movable Python lines remain",),
        ),
        PythonBoundaryTestCase(
            description="a reviewed native error handler is allowed",
            source_files={"compiler/keys.py": SWALLOWED},
            policy="remaining_lines = 8\n" + REVIEWED,
            expected_messages=("8 movable Python lines remain",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_policy_matching_source_when_checking_then_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: PythonBoundaryTestCase
) -> None:
    write_boundary_project(tmp_path, test_case)

    assert run_python_boundary_check(["--root", str(tmp_path)]) == 0
    output: str = capsys.readouterr().out
    assert all(message in output for message in test_case.expected_messages)


@pytest.mark.parametrize(
    "test_case",
    [
        PythonBoundaryTestCase(
            description="a re-export module with one extra assignment counts",
            source_files={"compiler/models.py": EXTRA_ASSIGNMENT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 7 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module with a function counts",
            source_files={"compiler/models.py": EXTRA_FUNCTION},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 10 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module importing from another module counts",
            source_files={"compiler/models.py": FOREIGN_IMPORT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 6 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module with a conditional import counts",
            source_files={"compiler/models.py": CONDITIONAL_IMPORT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 7 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module guarding an import with try counts",
            source_files={"compiler/models.py": GUARDED_IMPORT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 9 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module that renames a native name counts",
            source_files={"compiler/models.py": RENAMED_IMPORT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 6 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module with an unlisted native alias counts",
            source_files={"compiler/discovery/types.py": UNLISTED_ALIAS},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 4 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a listed native alias in another module counts",
            source_files={"compiler/sql_analysis/types.py": DISCOVERY_TYPES},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 4 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a listed native name under another alias counts",
            source_files={"compiler/discovery/types.py": WRONG_ALIAS},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 4 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="a re-export module whose __all__ lists another name counts",
            source_files={"compiler/models.py": EXTRA_EXPORT},
            policy="remaining_lines = 0\n",
            expected_messages=("Movable Python grew to 6 lines (policy: 0)",),
        ),
        PythonBoundaryTestCase(
            description="movable Python grew",
            source_files={"compiler/plan.py": "a = 1\nb = 2\nc = 3\n"},
            policy="remaining_lines = 2\n",
            expected_messages=("Movable Python grew to 3 lines (policy: 2)",),
        ),
        PythonBoundaryTestCase(
            description="movable Python shrank without lowering the baseline",
            source_files={"compiler/plan.py": "a = 1\n"},
            policy="remaining_lines = 2\n",
            expected_messages=("set remaining_lines = 1",),
        ),
        PythonBoundaryTestCase(
            description="an unreviewed native failure is swallowed",
            source_files={"compiler/keys.py": SWALLOWED},
            policy="remaining_lines = 8\n",
            expected_messages=("src/sqltide/compiler/keys.py:7 swallows a native failure",),
        ),
        PythonBoundaryTestCase(
            description="an aliased native module import swallows a failure",
            source_files={
                "compiler/keys.py": (
                    "import sqltide._native as _native\n"
                    "\n"
                    "\n"
                    "def digest(text):\n"
                    "    try:\n"
                    "        return _native.content_digest([text])\n"
                    "    except ValueError:\n"
                    "        return None\n"
                )
            },
            policy="remaining_lines = 8\n",
            expected_messages=("src/sqltide/compiler/keys.py:7 swallows a native failure",),
        ),
        PythonBoundaryTestCase(
            description="a handler that raises only conditionally",
            source_files={"compiler/keys.py": CONDITIONAL},
            policy="remaining_lines = 10\n",
            expected_messages=("src/sqltide/compiler/keys.py:7 swallows a native failure",),
        ),
        PythonBoundaryTestCase(
            description="a native wrapper call inside suppress in a method",
            source_files={"compiler/graph.py": SUPPRESSED},
            policy=(
                "remaining_lines = 10\n"
                '[[handled_native_errors]]\nlocation = "compiler/graph.py::build"\nnote = "Old."\n'
            ),
            expected_messages=(
                "src/sqltide/compiler/graph.py:8 swallows a native failure",
                "'compiler/graph.py::build' no longer matches any handler",
            ),
        ),
        PythonBoundaryTestCase(
            description="a staying path escapes the source root",
            source_files={"compiler/plan.py": "a = 1\n"},
            policy=('remaining_lines = 0\n[[stays]]\npath = ".."\nrule = 1\nnote = "No."\n'),
            expected_messages=("stays entry '..' must name a module under src/sqltide.",),
        ),
        PythonBoundaryTestCase(
            description="a reviewed handler no longer exists",
            source_files={"compiler/keys.py": RERAISED},
            policy="remaining_lines = 8\n" + REVIEWED,
            expected_messages=("'compiler/keys.py::digest' no longer matches any handler",),
        ),
        PythonBoundaryTestCase(
            description="a staying module is missing and has no rule or note",
            source_files={"compiler/plan.py": "a = 1\n"},
            policy='remaining_lines = 1\n[[stays]]\npath = "planner"\nnote = ""\n',
            expected_messages=(
                "stays entry 'planner' does not exist under src/sqltide.",
                "stays entry 'planner' needs a rule: one of 1 = executor or adapter runtime",
                "stays entry 'planner' needs a note with the evidence for its rule.",
            ),
        ),
        PythonBoundaryTestCase(
            description="a staying module has a rule outside 1 to 4",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=(
                'remaining_lines = 1\n[[stays]]\npath = "executor"\nrule = 5\nnote = "Runs."\n'
            ),
            expected_messages=("stays entry 'executor' has rule 5; use one of 1 = ",),
        ),
        PythonBoundaryTestCase(
            description="a staying module has a non-integer rule",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=(
                'remaining_lines = 1\n[[stays]]\npath = "executor"\nrule = "1"\nnote = "Runs."\n'
            ),
            expected_messages=("stays entry 'executor' has rule '1'; use one of 1 = ",),
        ),
        PythonBoundaryTestCase(
            description="a staying module has a boolean rule",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=(
                'remaining_lines = 1\n[[stays]]\npath = "executor"\nrule = true\nnote = "Runs."\n'
            ),
            expected_messages=("stays entry 'executor' has rule True; use one of 1 = ",),
        ),
        PythonBoundaryTestCase(
            description="a staying module has a whitespace-only note",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=('remaining_lines = 1\n[[stays]]\npath = "executor"\nrule = 1\nnote = "  "\n'),
            expected_messages=(
                "stays entry 'executor' needs a note with the evidence for its rule.",
            ),
        ),
        PythonBoundaryTestCase(
            description="a staying module still uses the replaced reason key",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=(
                'remaining_lines = 1\n[[stays]]\npath = "executor"\nreason = "executor"\n'
                'note = "Runs."\n'
            ),
            expected_messages=(
                "stays entry 'executor' has unknown key(s) reason; "
                "an entry has only path, rule and note.",
                "stays entry 'executor' needs a rule: one of ",
            ),
        ),
        PythonBoundaryTestCase(
            description="a staying module's note cites a different rule",
            source_files={"compiler/plan.py": "a = 1\n", "executor/run.py": "c = 3\n"},
            policy=(
                'remaining_lines = 1\n[[stays]]\npath = "executor"\nrule = 1\n'
                'note = "Rule 4 glue around a library."\n'
            ),
            expected_messages=("stays entry 'executor' has rule 1 but its note cites rule 4.",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_policy_violation_when_checking_then_fails_with_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: PythonBoundaryTestCase
) -> None:
    write_boundary_project(tmp_path, test_case)

    assert run_python_boundary_check(["--root", str(tmp_path)]) == 1
    errors: str = capsys.readouterr().err
    assert all(message in errors for message in test_case.expected_messages)


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
