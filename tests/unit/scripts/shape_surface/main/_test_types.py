from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShapeSurfaceTestCase:
    description: str
    recorded_source: str
    current_source: str
    expected_messages: tuple[str, ...]
    removed: dict[str, str] = field(default_factory=dict)
