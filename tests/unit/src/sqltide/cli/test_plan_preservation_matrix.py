from pathlib import Path
from textwrap import dedent, indent

import pytest

from sqltide._native import run_cli
from sqltide.adapter.models import CatalogColumn, CatalogRelation
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from tests.unit.src.sqltide.cli._test_types import CliPlanPreservationMatrixTestCase
from tests.unit.src.sqltide.cli.helpers import (
    AdapterConnectionProvider,
    RecordingAdapterConnection,
    handlers_with_overrides,
)


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanPreservationMatrixTestCase(
            description="plans managed offset replay through one catalog snapshot",
            replay_lineage_mode="offsets",
            pipeline_file_contents="""
source:
  kind: kafka
  name: order_events
  broker_list: kafka:9092
  topic: source.order_events
  replay_boundary:
    mode: offsets
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT kafka_key::String AS order_id,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=1,
        ),
        CliPlanPreservationMatrixTestCase(
            description="plans managed timestamp replay through one catalog snapshot",
            replay_lineage_mode="timestamp",
            pipeline_file_contents="""
source:
  kind: kafka
  name: order_events
  broker_list: kafka:9092
  topic: source.order_events
  replay_boundary:
    mode: timestamp
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT kafka_key::String AS order_id,
  _replay_timestamp::DateTime64(3) AS _replay_timestamp
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=1,
        ),
        CliPlanPreservationMatrixTestCase(
            description="plans managed landed-at replay through one catalog snapshot",
            replay_lineage_mode="landed_at",
            pipeline_file_contents="""
source:
  kind: kafka
  name: order_events
  broker_list: kafka:9092
  topic: source.order_events
  replay_boundary:
    mode: landed_at
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT kafka_key::String AS order_id,
  _replay_landed_at::DateTime64(3) AS _replay_landed_at
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=1,
        ),
        CliPlanPreservationMatrixTestCase(
            description="plans adopted offset replay through one catalog snapshot",
            replay_lineage_mode="offsets",
            pipeline_file_contents="""
source:
  kind: stream_table
  name: order_events
  table_name: orders_existing
  replay_boundary:
    mode: offsets
    columns:
      _replay_partition: event_partition
      _replay_offset: event_offset
      _replay_timestamp: event_timestamp
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT order_id::String AS order_id,
  _replay_partition::Int64 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(
                CatalogRelation(
                    name="orders_existing",
                    engine="MergeTree",
                    columns=(
                        CatalogColumn(name="order_id", type="String"),
                        CatalogColumn(name="event_partition", type="Int32"),
                        CatalogColumn(name="event_offset", type="Int64"),
                        CatalogColumn(name="event_timestamp", type="DateTime64(3)"),
                    ),
                ),
            ),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=2,
        ),
        CliPlanPreservationMatrixTestCase(
            description="plans adopted timestamp replay through one catalog snapshot",
            replay_lineage_mode="timestamp",
            pipeline_file_contents="""
source:
  kind: stream_table
  name: order_events
  table_name: orders_existing
  replay_boundary:
    mode: timestamp
    columns:
      _replay_timestamp: event_timestamp
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT order_id::String AS order_id,
  _replay_timestamp::DateTime64(3) AS _replay_timestamp
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(
                CatalogRelation(
                    name="orders_existing",
                    engine="MergeTree",
                    columns=(
                        CatalogColumn(name="order_id", type="String"),
                        CatalogColumn(name="event_timestamp", type="DateTime64(3)"),
                    ),
                ),
            ),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=2,
        ),
        CliPlanPreservationMatrixTestCase(
            description="plans adopted cursor replay through one catalog snapshot",
            replay_lineage_mode="cursor",
            pipeline_file_contents="""
source:
  kind: stream_table
  name: order_events
  table_name: orders_existing
  replay_boundary:
    mode: cursor
    columns:
      _replay_cursor: event_cursor
      _replay_timestamp: event_timestamp
""".lstrip(),
            model_file_contents="""
MODEL (
  engine "MergeTree()",
  order_by ["order_id"],
);
SELECT order_id::String AS order_id,
  _replay_cursor::UInt64 AS _replay_cursor
FROM __ref("order_events")
""".lstrip(),
            catalog_relations=(
                CatalogRelation(
                    name="orders_existing",
                    engine="MergeTree",
                    columns=(
                        CatalogColumn(name="order_id", type="String"),
                        CatalogColumn(name="event_cursor", type="UInt64"),
                        CatalogColumn(name="event_timestamp", type="DateTime64(3)"),
                    ),
                ),
            ),
            expected_exit_code=0,
            expected_subtree_summary="Subtrees to rebuild: 1",
            expected_catalog_load_count=1,
            expected_query_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_preservation_source_mode_when_running_plan_then_snapshot_path_stays_green(
    test_case: CliPlanPreservationMatrixTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    pipeline_dir: Path = project_dir / "pipelines" / "pl__order_events"
    pipeline_dir.mkdir(parents=True)
    (project_dir / "sqltide_project.toml").write_text(
        'name = "matrix_project"\ndefault_target = "test"\n\n'
        '[defaults]\npipeline_mode = "virtual"\n\n'
        '[targets.test]\ndatabase = "analytics"\n\n'
        '[targets.test.connection]\nhost = "localhost"\nport = 8123\n'
        'username = "sqltide"\npassword = "sqltide"\n',
        encoding="utf-8",
    )
    source_body: str = dedent(dedent(test_case.pipeline_file_contents).removeprefix("source:\n"))
    source_dir: Path = project_dir / "sources"
    source_dir.mkdir()
    (source_dir / "order_events.yml").write_text(
        "sources:\n  - " + indent(source_body, "    ").lstrip(),
        encoding="utf-8",
    )
    (pipeline_dir / "orders_enriched.sql").write_text(
        test_case.model_file_contents,
        encoding="utf-8",
    )
    connection: RecordingAdapterConnection = RecordingAdapterConnection(
        relations=test_case.catalog_relations
    )
    provider: AdapterConnectionProvider = AdapterConnectionProvider(connection)
    monkeypatch.setattr(ClickHouseAdapter, "connect", provider)

    exit_code: int = run_cli(
        argv=("sqt", "plan"),
        handlers=handlers_with_overrides(),
        environment={},
        working_directory=project_dir,
    )
    captured_stdout: str = capsys.readouterr().out

    assert exit_code == test_case.expected_exit_code
    assert test_case.expected_subtree_summary in captured_stdout
    assert len(connection.catalog_databases) == test_case.expected_catalog_load_count
    assert len(connection.statements) == test_case.expected_query_count
