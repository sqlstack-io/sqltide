"""Public SQL command contract cases."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RulesTestCase:
    description: str
    config: str = ""
    header: str = "kind view"
    flags: tuple[str, ...] = ()
    expected_exit: int = 0
    expected_codes: tuple[str, ...] = ()
    expected_error: str = ""
    expected_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FormatTestCase:
    description: str
    sql: str
    expected_fragments: tuple[str, ...]
    newline: str = "\n"
    flags: tuple[str, ...] = ()
