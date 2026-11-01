from pathlib import Path
from typing import cast

import pytest

from sqltide.compiler.discovery._helpers.source_registry import discover_source_registry
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.main.load_project_input_for_path import (
    load_project_input_for_path,
)
from sqltide.compiler.discovery.models import (
    DiscoveredSourceFile,
    ExternalTableSourceStep,
    KafkaLandingStep,
    LoadedProject,
    PostgresRefreshSourceStep,
    ReplayBoundary,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import SourceKind
from tests.unit.src.sqltide.compiler.discovery._test_types import (
    KafkaBrokerDefaultTestCase,
    KafkaSourceNamingMacroErrorTestCase,
    KafkaSourceNamingMacroSuccessTestCase,
    PostgresSourceRejectionTestCase,
    PostgresSourceTestCase,
    ProjectKafkaBrokerDefaultTestCase,
    SourceBoundaryModeTestCase,
    SourceFreshnessTestCase,
    SourceRegistryErrorTestCase,
    SourceRegistryTestCase,
    SourceRetentionInterpolationTestCase,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import (
    flatten_source_registry,
    write_source_yml,
)

_KAFKA_SOURCE_WITH_FRESHNESS: str = """
sources:
  - name: orders
    kind: kafka
    broker_list: broker:9092
    topic: orders.live
    replay_boundary:
      mode: offsets
    freshness:
      warn_after: 15m
      error_after: 1h
"""

_KAFKA_SOURCE_WITHOUT_FRESHNESS: str = """
sources:
  - name: orders
    kind: kafka
    broker_list: broker:9092
    topic: orders.live
    replay_boundary:
      mode: offsets
"""

_ADOPTED_SOURCE_WITH_FRESHNESS: str = """
sources:
  - name: orders
    kind: stream_table
    table_name: raw_orders
    replay_boundary:
      mode: cursor
      columns:
        _replay_cursor: event_cursor
        _replay_timestamp: event_timestamp
    freshness:
      error_after: 2d
"""


@pytest.mark.parametrize(
    "test_case",
    [
        SourceRegistryTestCase(
            description="loads managed and adopted sources once in stable file order",
            expected_source_names=("orders", "external_orders"),
            expected_boundary_modes=("offsets", "cursor"),
            expected_relative_paths=("sources/a.yml", "sources/z.yml"),
            expected_managed_source_ttl="_replay_landed_at + INTERVAL 14 DAY",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_source_files_when_discovering_then_returns_stable_typed_registry(
    test_case: SourceRegistryTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="z.yml",
        contents="""
        sources:
          - name: external_orders
            kind: stream_table
            table_name: "${table_name}"
            replay_boundary:
              mode: cursor
              columns:
                _replay_cursor: event_cursor
                _replay_timestamp: event_timestamp
        """,
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="a.yml",
        contents="""
        sources:
          - name: orders
            kind: kafka
            broker_list: "${ENV:BROKER_LIST}"
            topic: source.orders
            ttl: "_replay_landed_at + INTERVAL ${ttl_days} DAY"
            replay_boundary:
              mode: offsets
        """,
    )

    source_files: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables={"table_name": "orders_existing", "ttl_days": 14},
        environment={"BROKER_LIST": "redpanda:9092"},
    )
    sources: tuple[KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep, ...] = (
        flatten_source_registry(source_files)
    )

    assert tuple(source.name for source in sources) == test_case.expected_source_names
    assert (
        tuple(
            cast(ReplayBoundary, cast(KafkaLandingStep, source).replay_boundary).mode
            for source in sources
        )
        == test_case.expected_boundary_modes
    )
    assert tuple(str(item.source_file.relative_path) for item in source_files) == (
        test_case.expected_relative_paths
    )
    managed_source: KafkaLandingStep = cast(KafkaLandingStep, sources[0])
    assert managed_source.kafka.ttl == test_case.expected_managed_source_ttl


@pytest.mark.parametrize(
    "test_case",
    [
        SourceRetentionInterpolationTestCase(
            description="source typed retention interpolates variables and environment",
            expected_duration_seconds=43_200,
            expected_fallback="landed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_interpolated_source_retention_when_discovering_then_policy_is_normalized(
    test_case: SourceRetentionInterpolationTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - name: events
            kind: kafka
            broker_list: kafka:9092
            topic: source.events
            retention:
              duration: "${retention_duration}"
              timestamp: broker
              fallback: "${ENV:RETENTION_FALLBACK}"
            replay_boundary: {mode: offsets}
        """,
    )

    source_files: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables={"retention_duration": "12h"},
        environment={"RETENTION_FALLBACK": "landed"},
    )
    source: KafkaLandingStep = cast(KafkaLandingStep, flatten_source_registry(source_files)[0])

    assert source.kafka.retention is not None and source.kafka.retention is not False
    assert source.kafka.retention.duration_seconds == test_case.expected_duration_seconds
    assert str(source.kafka.retention.fallback) == test_case.expected_fallback


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaBrokerDefaultTestCase(
            description="project brokers supply an omitted source value",
            source_broker_yaml="",
            default_broker_list="kafka1:9092,kafka2:9092",
            expected_broker_list="kafka1:9092,kafka2:9092",
        ),
        KafkaBrokerDefaultTestCase(
            description="source brokers override the project default",
            source_broker_yaml="broker_list: source-kafka:9092",
            default_broker_list="project-kafka:9092",
            expected_broker_list="source-kafka:9092",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_kafka_broker_defaults_when_discovering_source_then_applies_precedence(
    test_case: KafkaBrokerDefaultTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents=f"""
        sources:
          - name: events
            kind: kafka
            {test_case.source_broker_yaml}
            topic: source.events
            replay_boundary: {{mode: offsets}}
        """,
    )

    source_files: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables={},
        environment={},
        default_kafka_broker_list=test_case.default_broker_list,
    )

    source: KafkaLandingStep = cast(KafkaLandingStep, flatten_source_registry(source_files)[0])
    assert source.kafka.broker_list == test_case.expected_broker_list


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectKafkaBrokerDefaultTestCase(
            description="interpolated project brokers reach source discovery",
            configured_broker_list="${ENV:KAFKA_BROKERS}",
            environment=(("KAFKA_BROKERS", "kafka1:9092,kafka2:9092"),),
            expected_broker_list="kafka1:9092,kafka2:9092",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_interpolated_project_kafka_default_when_loading_project_then_source_inherits_it(
    test_case: ProjectKafkaBrokerDefaultTestCase,
    tmp_path: Path,
) -> None:
    (tmp_path / "sqltide_project.toml").write_text(
        f"""
name = "events"
default_target = "test"

[defaults]
kafka_broker_list = "{test_case.configured_broker_list}"

[targets.test]
""".strip(),
        encoding="utf-8",
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - name: events
            kind: kafka
            topic: source.events
            replay_boundary: {mode: offsets}
        """,
    )

    loaded: LoadedProject | None = load_project_input_for_path(
        path=tmp_path,
        environment=dict(test_case.environment),
    )

    assert loaded is not None
    source: KafkaLandingStep = cast(
        KafkaLandingStep, flatten_source_registry(loaded.source_files)[0]
    )
    assert source.kafka.broker_list == test_case.expected_broker_list


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaSourceNamingMacroSuccessTestCase(
            description="derives a source name after topic interpolation",
            expected_name="orders",
            expected_topic="source.orders.created",
            expected_origin="derived",
            expected_macro_name="kafka_source_name",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_omitted_kafka_name_when_loading_project_then_macro_derives_interpolated_topic_name(
    test_case: KafkaSourceNamingMacroSuccessTestCase,
    tmp_path: Path,
) -> None:
    (tmp_path / "sqltide_project.toml").write_text(
        """
name = "events"
default_target = "test"

[vars]
topic = "source.orders.created"

[defaults.sources.kafka]
naming_macro = "kafka_source_name"

[targets.test]
""".strip(),
        encoding="utf-8",
    )
    macros_dir: Path = tmp_path / "macros"
    macros_dir.mkdir()
    (macros_dir / "source_names.py").write_text(
        "def kafka_source_name(topic: str) -> str:\n    return topic.split('.')[1]\n",
        encoding="utf-8",
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - kind: kafka
            broker_list: redpanda:9092
            topic: "${topic}"
            replay_boundary: {mode: offsets}
        """,
    )

    loaded: LoadedProject | None = load_project_input_for_path(path=tmp_path)

    assert loaded is not None
    source: KafkaLandingStep = cast(
        KafkaLandingStep, flatten_source_registry(loaded.source_files)[0]
    )
    assert source.name == test_case.expected_name
    assert source.kafka.topic == test_case.expected_topic
    assert source.name_origin == test_case.expected_origin
    assert source.naming_macro == test_case.expected_macro_name
    assert source.naming_macro_fingerprint is not None
    assert len(source.naming_macro_fingerprint) == 64

    first_fingerprint: str = source.naming_macro_fingerprint
    (macros_dir / "source_names.py").write_text(
        (
            "def kafka_source_name(topic: str) -> str:\n"
            "    return topic.split('.')[1] if topic else 'unused'\n"
        ),
        encoding="utf-8",
    )
    reloaded: LoadedProject | None = load_project_input_for_path(path=tmp_path)
    assert reloaded is not None
    reloaded_source: KafkaLandingStep = cast(
        KafkaLandingStep, flatten_source_registry(reloaded.source_files)[0]
    )
    assert reloaded_source.name == source.name
    assert reloaded_source.naming_macro_fingerprint != first_fingerprint


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaSourceNamingMacroSuccessTestCase(
            description="keeps an explicit source name without calling the macro",
            expected_name="authored_events",
            expected_topic="source.events",
            expected_origin="explicit",
            expected_macro_name=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_kafka_name_when_naming_macro_would_fail_then_explicit_name_wins(
    test_case: KafkaSourceNamingMacroSuccessTestCase,
    tmp_path: Path,
) -> None:
    (tmp_path / "sqltide_project.toml").write_text(
        """
name = "events"
default_target = "test"

[defaults.sources.kafka]
naming_macro = "kafka_source_name"

[targets.test]
""".strip(),
        encoding="utf-8",
    )
    macros_dir: Path = tmp_path / "macros"
    macros_dir.mkdir()
    (macros_dir / "source_names.py").write_text(
        "def kafka_source_name(topic: str) -> str:\n    raise RuntimeError('not called')\n",
        encoding="utf-8",
    )
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents="""
        sources:
          - name: authored_events
            kind: kafka
            broker_list: redpanda:9092
            topic: source.events
            replay_boundary: {mode: offsets}
        """,
    )

    loaded: LoadedProject | None = load_project_input_for_path(path=tmp_path)

    assert loaded is not None
    source: KafkaLandingStep = cast(
        KafkaLandingStep, flatten_source_registry(loaded.source_files)[0]
    )
    assert source.name == test_case.expected_name
    assert source.kafka.topic == test_case.expected_topic
    assert source.name_origin == test_case.expected_origin
    assert source.naming_macro == test_case.expected_macro_name
    assert source.naming_macro_fingerprint is None


@pytest.mark.parametrize(
    "test_case",
    [
        KafkaSourceNamingMacroErrorTestCase(
            description="rejects an unknown configured macro",
            macro_name="missing_naming_macro",
            macro_source="def other_macro(topic: str) -> str:\n    return 'events'\n",
            sources_yaml="""
            sources:
              - kind: kafka
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: offsets}
            """,
            expected_error_fragment="unknown Kafka naming macro 'missing_naming_macro'",
        ),
        KafkaSourceNamingMacroErrorTestCase(
            description="rejects a non-identifier macro result",
            macro_name="kafka_source_name",
            macro_source=(
                "def kafka_source_name(topic: str) -> str:\n    return 'source.events'\n"
            ),
            sources_yaml="""
            sources:
              - kind: kafka
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: offsets}
            """,
            expected_error_fragment="must resolve to an unqualified identifier",
        ),
        KafkaSourceNamingMacroErrorTestCase(
            description="rejects collisions between derived names",
            macro_name="kafka_source_name",
            macro_source="def kafka_source_name(topic: str) -> str:\n    return 'events'\n",
            sources_yaml="""
            sources:
              - kind: kafka
                broker_list: redpanda:9092
                topic: source.events.one
                replay_boundary: {mode: offsets}
              - kind: kafka
                broker_list: redpanda:9092
                topic: source.events.two
                replay_boundary: {mode: offsets}
            """,
            expected_error_fragment="Duplicate source name 'events'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_kafka_naming_macro_case_when_loading_project_then_reports_source_error(
    test_case: KafkaSourceNamingMacroErrorTestCase,
    tmp_path: Path,
) -> None:
    (tmp_path / "sqltide_project.toml").write_text(
        f"""
name = "events"
default_target = "test"

[defaults.sources.kafka]
naming_macro = "{test_case.macro_name}"

[targets.test]
""".strip(),
        encoding="utf-8",
    )
    macros_dir: Path = tmp_path / "macros"
    macros_dir.mkdir()
    (macros_dir / "source_names.py").write_text(test_case.macro_source, encoding="utf-8")
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents=test_case.sources_yaml,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        load_project_input_for_path(path=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        SourceBoundaryModeTestCase(
            description="loads managed offset boundary",
            source_contents="""
            sources:
              - name: events
                kind: kafka
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: offsets}
            """,
            expected_source_type_name="KafkaLandingStep",
            expected_mode="offsets",
            expected_columns=(None, None, None, None, None),
        ),
        SourceBoundaryModeTestCase(
            description="loads managed timestamp boundary",
            source_contents="""
            sources:
              - name: events
                kind: kafka
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: timestamp}
            """,
            expected_source_type_name="KafkaLandingStep",
            expected_mode="timestamp",
            expected_columns=(None, None, None, None, None),
        ),
        SourceBoundaryModeTestCase(
            description="interpolates managed kind and timestamp boundary mode",
            source_contents="""
            sources:
              - name: events
                kind: "${source_kind}"
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: "${boundary_mode}"}
            """,
            expected_source_type_name="KafkaLandingStep",
            expected_mode="timestamp",
            expected_columns=(None, None, None, None, None),
            variables=(("boundary_mode", "timestamp"), ("source_kind", "kafka")),
        ),
        SourceBoundaryModeTestCase(
            description="loads managed landed-at boundary",
            source_contents="""
            sources:
              - name: events
                kind: kafka
                broker_list: redpanda:9092
                topic: source.events
                replay_boundary: {mode: landed_at}
            """,
            expected_source_type_name="KafkaLandingStep",
            expected_mode="landed_at",
            expected_columns=(None, None, None, None, None),
        ),
        SourceBoundaryModeTestCase(
            description="loads adopted offset boundary roles",
            source_contents="""
            sources:
              - name: events
                kind: stream_table
                table_name: events_existing
                replay_boundary:
                  mode: offsets
                  columns:
                    _replay_partition: physical_partition
                    _replay_offset: physical_offset
                    _replay_timestamp: physical_timestamp
            """,
            expected_source_type_name="ExternalTableSourceStep",
            expected_mode="offsets",
            expected_columns=(
                "physical_partition",
                "physical_offset",
                "physical_timestamp",
                None,
                None,
            ),
        ),
        SourceBoundaryModeTestCase(
            description="loads adopted timestamp boundary role",
            source_contents="""
            sources:
              - name: events
                kind: stream_table
                table_name: events_existing
                replay_boundary:
                  mode: timestamp
                  columns: {_replay_timestamp: physical_timestamp}
            """,
            expected_source_type_name="ExternalTableSourceStep",
            expected_mode="timestamp",
            expected_columns=(None, None, "physical_timestamp", None, None),
        ),
        SourceBoundaryModeTestCase(
            description="loads adopted cursor boundary roles",
            source_contents="""
            sources:
              - name: events
                kind: stream_table
                table_name: events_existing
                replay_boundary:
                  mode: cursor
                  columns:
                    _replay_cursor: physical_cursor
                    _replay_timestamp: physical_timestamp
            """,
            expected_source_type_name="ExternalTableSourceStep",
            expected_mode="cursor",
            expected_columns=(None, None, "physical_timestamp", None, "physical_cursor"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_supported_source_mode_when_discovering_then_it_retains_boundary_contract(
    test_case: SourceBoundaryModeTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="events.yml",
        contents=test_case.source_contents,
    )

    source_files: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables=dict(test_case.variables),
        environment={},
    )
    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep = source_files[
        0
    ].sources[0]
    boundary: ReplayBoundary = cast(ReplayBoundary, cast(KafkaLandingStep, source).replay_boundary)

    assert type(source).__name__ == test_case.expected_source_type_name
    assert boundary.mode == test_case.expected_mode
    assert (
        boundary.columns.partition,
        boundary.columns.offset,
        boundary.columns.timestamp,
        boundary.columns.landed_at,
        boundary.columns.cursor,
    ) == test_case.expected_columns


@pytest.mark.parametrize(
    "test_case",
    [
        SourceRegistryErrorTestCase(
            description="rejects duplicate source names with both authored paths",
            source_files=(
                (
                    "a.yml",
                    """
                    sources:
                      - name: orders
                        kind: kafka
                        broker_list: redpanda:9092
                        topic: source.orders
                        replay_boundary: {mode: offsets}
                    """,
                ),
                (
                    "b.yml",
                    """
                    sources:
                      - name: orders
                        kind: kafka
                        broker_list: redpanda:9092
                        topic: source.other
                        replay_boundary: {mode: timestamp}
                    """,
                ),
            ),
            expected_error_fragment=(
                "Duplicate source name 'orders' found in .*sources/a.yml.*sources/b.yml"
            ),
        ),
        SourceRegistryErrorTestCase(
            description="rejects managed and adopted field mixing",
            source_files=(
                (
                    "mixed.yml",
                    """
                    sources:
                      - name: orders
                        kind: stream_table
                        table_name: orders_existing
                        topic: source.orders
                        replay_boundary:
                          mode: timestamp
                          columns: {_replay_timestamp: event_timestamp}
                    """,
                ),
            ),
            expected_error_fragment="must not mix adopted and Kafka fields: topic",
        ),
        SourceRegistryErrorTestCase(
            description="rejects TTL on an adopted source",
            source_files=(
                (
                    "adopted_ttl.yml",
                    """
                    sources:
                      - name: orders
                        kind: stream_table
                        table_name: orders_existing
                        ttl: _replay_landed_at + INTERVAL 7 DAY
                        replay_boundary:
                          mode: timestamp
                          columns: {_replay_timestamp: event_timestamp}
                    """,
                ),
            ),
            expected_error_fragment="must not mix adopted and Kafka fields: ttl",
        ),
        SourceRegistryErrorTestCase(
            description="rejects managed cursor mode",
            source_files=(
                (
                    "managed_cursor.yml",
                    """
                sources:
                  - name: events
                    kind: kafka
                    broker_list: redpanda:9092
                    topic: source.events
                    replay_boundary: {mode: cursor}
                """,
                ),
            ),
            expected_error_fragment="mode 'cursor' must be one of",
        ),
        SourceRegistryErrorTestCase(
            description="rejects adopted landed-at mode",
            source_files=(
                (
                    "adopted_landed.yml",
                    """
                sources:
                  - name: events
                    kind: stream_table
                    table_name: events_existing
                    replay_boundary:
                      mode: landed_at
                      columns: {_replay_landed_at: physical_landed_at}
                """,
                ),
            ),
            expected_error_fragment="mode 'landed_at' must be one of",
        ),
        SourceRegistryErrorTestCase(
            description="rejects missing adopted offset roles",
            source_files=(
                (
                    "missing_roles.yml",
                    """
                sources:
                  - name: events
                    kind: stream_table
                    table_name: events_existing
                    replay_boundary:
                      mode: offsets
                      columns: {_replay_partition: physical_partition}
                """,
                ),
            ),
            expected_error_fragment="offsets mode requires partition, offset, and timestamp",
        ),
        SourceRegistryErrorTestCase(
            description="rejects unknown source fields with source path",
            source_files=(
                (
                    "unknown.yml",
                    """
                sources:
                  - name: events
                    kind: kafka
                    broker_list: redpanda:9092
                    topic: source.events
                    replay_boundary: {mode: offsets}
                    batch_size: 10
                """,
                ),
            ),
            expected_error_fragment="sources/unknown.yml.*unsupported keys: batch_size",
        ),
        SourceRegistryErrorTestCase(
            description="rejects interpolation in source setting keys",
            source_files=(
                (
                    "key.yml",
                    """
                sources:
                  - name: events
                    kind: kafka
                    broker_list: redpanda:9092
                    topic: source.events
                    replay_boundary: {mode: offsets}
                    settings: {"${setting_name}": "2"}
                """,
                ),
            ),
            expected_error_fragment="must not interpolate mapping keys",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_source_registry_when_discovering_then_rejects_with_paths(
    test_case: SourceRegistryErrorTestCase,
    tmp_path: Path,
) -> None:
    relative_path: str
    contents: str
    for relative_path, contents in test_case.source_files:
        write_source_yml(
            project_dir=tmp_path,
            relative_path=relative_path,
            contents=contents,
        )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        discover_source_registry(project_dir=tmp_path, variables={}, environment={})


@pytest.mark.parametrize(
    "test_case",
    [
        SourceFreshnessTestCase(
            description="parses a managed source freshness policy",
            source_yaml=_KAFKA_SOURCE_WITH_FRESHNESS,
            default_freshness=None,
            expected_freshness=SourceFreshnessPolicy(warn_after="15m", error_after="1h"),
        ),
        SourceFreshnessTestCase(
            description="parses an adopted source freshness policy",
            source_yaml=_ADOPTED_SOURCE_WITH_FRESHNESS,
            default_freshness=None,
            expected_freshness=SourceFreshnessPolicy(error_after="2d"),
        ),
        SourceFreshnessTestCase(
            description="falls back to the project default freshness policy",
            source_yaml=_KAFKA_SOURCE_WITHOUT_FRESHNESS,
            default_freshness=SourceFreshnessPolicy(warn_after="30m"),
            expected_freshness=SourceFreshnessPolicy(warn_after="30m"),
        ),
        SourceFreshnessTestCase(
            description="prefers the source policy over the project default",
            source_yaml=_KAFKA_SOURCE_WITH_FRESHNESS,
            default_freshness=SourceFreshnessPolicy(warn_after="30m"),
            expected_freshness=SourceFreshnessPolicy(warn_after="15m", error_after="1h"),
        ),
        SourceFreshnessTestCase(
            description="defaults an absent freshness policy to none",
            source_yaml=_KAFKA_SOURCE_WITHOUT_FRESHNESS,
            default_freshness=None,
            expected_freshness=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_freshness_config_when_discovering_then_returns_expected_policy(
    test_case: SourceFreshnessTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(project_dir=tmp_path, relative_path="a.yml", contents=test_case.source_yaml)

    registry: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables={},
        environment={},
        default_freshness=test_case.default_freshness,
    )

    source: KafkaLandingStep | ExternalTableSourceStep | PostgresRefreshSourceStep = (
        flatten_source_registry(registry)[0]
    )
    assert source.freshness == test_case.expected_freshness


@pytest.mark.parametrize(
    "test_case",
    [
        PostgresSourceTestCase(
            description="a postgres source keeps its connection identity and refresh cadence",
            sources_yaml="""
            sources:
              - name: inventory__course
                kind: postgres
                host: warehouse.example.com
                database: inventory
                table: course
                user: readonly
                password_env: INVENTORY_READONLY_PASSWORD
                refresh: 1 HOUR
            """,
            expected_host="warehouse.example.com",
            expected_port=5432,
            expected_refresh="1 HOUR",
            expected_password_env="INVENTORY_READONLY_PASSWORD",
            expected_append=True,
        ),
        PostgresSourceTestCase(
            description="an explicit port and append flag override the defaults",
            sources_yaml="""
            sources:
              - name: inventory__entry
                kind: postgres
                host: pg.internal
                port: 6543
                database: inventory
                table: entry
                user: readonly
                refresh: 5 MINUTE
                append: false
            """,
            expected_host="pg.internal",
            expected_port=6543,
            expected_refresh="5 MINUTE",
            expected_password_env=None,
            expected_append=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_postgres_source_when_discovering_then_refresh_identity_is_retained(
    test_case: PostgresSourceTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="inventory.yml",
        contents=test_case.sources_yaml,
    )

    registry: tuple[DiscoveredSourceFile, ...] = discover_source_registry(
        project_dir=tmp_path,
        variables={},
        environment={},
    )
    source: PostgresRefreshSourceStep = cast(
        PostgresRefreshSourceStep, flatten_source_registry(registry)[0]
    )

    assert source.kind == SourceKind.POSTGRES
    assert source.host == test_case.expected_host
    assert source.port == test_case.expected_port
    assert source.refresh == test_case.expected_refresh
    assert source.password_env == test_case.expected_password_env
    assert source.append is test_case.expected_append


@pytest.mark.parametrize(
    "test_case",
    [
        PostgresSourceRejectionTestCase(
            description="a postgres source rejects a refresh that is not an interval",
            sources_yaml="""
            sources:
              - name: inventory__course
                kind: postgres
                host: pg.internal
                database: inventory
                table: course
                user: readonly
                refresh: hourly
            """,
            expected_error_fragment="refresh must be an interval",
        ),
        PostgresSourceRejectionTestCase(
            description="a postgres source rejects streaming fields",
            sources_yaml="""
            sources:
              - name: inventory__course
                kind: postgres
                host: pg.internal
                database: inventory
                table: course
                user: readonly
                refresh: 1 HOUR
                topic: source.inventory.course
            """,
            expected_error_fragment="must not declare streaming fields",
        ),
        PostgresSourceRejectionTestCase(
            description="a postgres source rejects a missing connection field",
            sources_yaml="""
            sources:
              - name: inventory__course
                kind: postgres
                host: pg.internal
                table: course
                user: readonly
                refresh: 1 HOUR
            """,
            expected_error_fragment="database",
        ),
        PostgresSourceRejectionTestCase(
            description="a postgres source rejects a superscript digit port",
            sources_yaml="""
            sources:
              - name: inventory__course
                kind: postgres
                host: pg.internal
                database: inventory
                table: course
                user: readonly
                refresh: 1 HOUR
                port: "²"
            """,
            expected_error_fragment="port must be a whole number",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_postgres_source_when_discovering_then_reports_the_reason(
    test_case: PostgresSourceRejectionTestCase,
    tmp_path: Path,
) -> None:
    write_source_yml(
        project_dir=tmp_path,
        relative_path="inventory.yml",
        contents=test_case.sources_yaml,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        discover_source_registry(project_dir=tmp_path, variables={}, environment={})
