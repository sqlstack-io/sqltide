from dataclasses import dataclass


@dataclass(frozen=True)
class PlaygroundTestCase:
    description: str
    directory_name: str = "sqltide-playground"
    arguments: tuple[str, ...] = ()
    expected_exit_code: int = 0
