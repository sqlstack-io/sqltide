"""Execute one project macro file and collect its public macro functions."""

from types import ModuleType

from sqltide.compiler.discovery.models import DiscoveredProjectFile
from sqltide.compiler.macros._helpers.registry import collect_public_macros, execute_macro_module
from sqltide.compiler.macros.models import LoadedMacro


def load_macro_file(*, macro_file: DiscoveredProjectFile) -> tuple[LoadedMacro, ...]:
    """Run one macro module's source and return its public synchronous functions by name."""

    module: ModuleType = execute_macro_module(macro_file=macro_file)
    return collect_public_macros(module=module, macro_file=macro_file)
