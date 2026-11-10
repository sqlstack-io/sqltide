import json

import pytest

from sqltide._native import (
    build_normalized_fingerprint,
)
from sqltide.compiler.compile.models import KafkaSettings, KafkaTableSpec
from tests.unit.src.sqltide.compiler.planner.main._test_types import (
    NormalizedFingerprintTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        NormalizedFingerprintTestCase(
            description="serializes immutable Kafka settings deterministically",
            settings=(
                ("kafka_skip_broken_messages", "10"),
                ("kafka_num_consumers", "1"),
            ),
            expected_settings={
                "kafka_num_consumers": "1",
                "kafka_skip_broken_messages": "10",
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dataclass_with_immutable_mapping_when_fingerprinting_then_serializes_canonical_json(
    test_case: NormalizedFingerprintTestCase,
) -> None:
    spec: KafkaTableSpec = KafkaTableSpec(
        columns=(),
        kafka=KafkaSettings(
            broker_list="kafka:9092",
            topic="source.legacy_partner.heartbeats",
            consumer_group="heartbeats",
            format="JSONAsString",
            settings=dict(test_case.settings),
        ),
        naming_macro_fingerprint="macro-fingerprint",
    )

    payload: dict[str, object] = json.loads(build_normalized_fingerprint(spec))

    assert payload["kafka"]["settings"] == test_case.expected_settings
    assert payload["naming_macro_fingerprint"] == "macro-fingerprint"


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
