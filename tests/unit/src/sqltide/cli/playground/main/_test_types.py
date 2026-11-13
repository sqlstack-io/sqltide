from dataclasses import dataclass


@dataclass(frozen=True)
class PlaygroundTestCase:
    description: str
    directory_name: str = "sqltide-playground"
    arguments: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = (
        "compose.yaml",
        "consumer.sql",
        "revisions/completed_orders.sql",
    )
    expected_exit_code: int = 0


@dataclass(frozen=True)
class PlaygroundStartFailureTestCase:
    description: str
    expected_exit_code: int
    expected_message: str
