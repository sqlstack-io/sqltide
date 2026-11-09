from dataclasses import dataclass


@dataclass(frozen=True)
class DeriveTransformOutputColumnsErrorTestCase:
    description: str
    query: str
    expected_error_type: type[Exception]
    expected_message_fragments: tuple[str, ...]
    expected_error_attributes: dict[str, object]
