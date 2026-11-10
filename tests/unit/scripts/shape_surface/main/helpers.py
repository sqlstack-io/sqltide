import importlib
import sys
from pathlib import Path
from types import ModuleType

from scripts.shape_surface._helpers.inventory import describe_module

FIXTURE_MODULE: str = "shape_surface_fixture"


def record_fixture(*, root: Path, source: str) -> dict[str, dict[str, object]]:
    """Import `source` as the fixture module and record its public surface."""
    return describe_module(load_fixture(root=root, source=source))


def load_fixture(*, root: Path, source: str) -> ModuleType:
    """Replace the fixture module with `source`, as a later release of the package would."""
    (root / f"{FIXTURE_MODULE}.py").write_text(source, encoding="utf-8")
    sys.modules.pop(FIXTURE_MODULE, None)
    importlib.invalidate_caches()
    return importlib.import_module(FIXTURE_MODULE)
