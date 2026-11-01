from dataclasses import dataclass


@dataclass(frozen=True)
class PlaygroundLifecycleTestCase:
    description: str
    expected_initial_rows: tuple[tuple[int, str, int], ...]
    expected_candidate_rows: tuple[tuple[int, str, int], ...]
    expected_live_rows: tuple[tuple[int, str, int], ...]
    expected_promoted_rows: tuple[tuple[int, str, int], ...]
