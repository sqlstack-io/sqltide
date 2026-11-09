from pathlib import Path

import pytest

from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import CompiledTableModel
from sqltide.compiler.discovery._helpers.load import load_pipeline_directory
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.types import PipelineMode, RowErrorPolicy
from tests.unit.src.sqltide.compiler.compile._test_types import (
    PipelineRowErrorPolicyTestCase,
    RejectRowErrorPolicyTestCase,
    ResolveRowErrorPolicyTestCase,
    RowErrorHeaderRejectedTestCase,
)
from tests.unit.src.sqltide.compiler.compile.helpers import (
    build_row_error_model,
    write_registry_pipeline_project,
)

_SOURCE_CONTENTS: str = """
source:
  kind: stream_table
  name: orders
  table_name: orders_existing
  replay_boundary:
    mode: offsets
    columns:
      _replay_partition: event_partition
      _replay_offset: event_offset
      _replay_timestamp: event_timestamp
"""
_ROW_LEVEL_SELECT: str = (
    "SELECT JSONExtractString(kafka_value, 'id')::String AS order_id, "
    '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
)
_GROUPED_SELECT: str = (
    "SELECT toUInt64(count())::UInt64 AS order_count, "
    '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders") '
    "GROUP BY _replay_timestamp"
)
_WINDOW_SELECT: str = (
    "SELECT (row_number() OVER (ORDER BY kafka_value))::UInt64 AS order_rank, "
    '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
)
_INNER_AGGREGATE_SELECT: str = (
    "SELECT order_id::String AS order_id, "
    "_replay_timestamp::DateTime64(3) AS _replay_timestamp "
    "FROM (SELECT max(kafka_key) AS order_id, max(_replay_timestamp) AS _replay_timestamp "
    'FROM __source("orders"))'
)


@pytest.mark.parametrize(
    "test_case",
    [
        ResolveRowErrorPolicyTestCase(
            description="unset everywhere stays on fail",
            model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
            project_policy=None,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="project default enables an eligible model",
            model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="model value wins over the project default",
            model_contents=f"MODEL (on_row_error fail); {_ROW_LEVEL_SELECT}",
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="explicit quarantine applies without a project default",
            model_contents=f"MODEL (on_row_error quarantine); {_ROW_LEVEL_SELECT}",
            project_policy=None,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="array column stays eligible and unchecked",
            model_contents=(
                "MODEL (on_row_error quarantine); SELECT kafka_key::Array(String) AS tags, "
                '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
            ),
            project_policy=None,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="self-shadowing column stays eligible",
            model_contents=(
                "MODEL (on_row_error quarantine); SELECT kafka_key::String AS kafka_key, "
                '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
            ),
            project_policy=None,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="an aggregate only in a nested query stays eligible",
            model_contents=f"MODEL (on_row_error quarantine); {_INNER_AGGREGATE_SELECT}",
            project_policy=None,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="inherited quarantine leaves a grouped model on fail",
            model_contents=f"MODEL (); {_GROUPED_SELECT}",
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="inherited quarantine leaves a virtual pipeline on fail",
            model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.VIRTUAL,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="pipeline value wins over the project default",
            model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
            project_policy=RowErrorPolicy.FAIL,
            pipeline_policy=RowErrorPolicy.QUARANTINE,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        ResolveRowErrorPolicyTestCase(
            description="pipeline fail overrides a project quarantine",
            model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=RowErrorPolicy.FAIL,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="model fail wins over a pipeline quarantine",
            model_contents=f"MODEL (on_row_error fail); {_ROW_LEVEL_SELECT}",
            project_policy=None,
            pipeline_policy=RowErrorPolicy.QUARANTINE,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="pipeline quarantine leaves an ineligible model on fail silently",
            model_contents=f"MODEL (); {_GROUPED_SELECT}",
            project_policy=None,
            pipeline_policy=RowErrorPolicy.QUARANTINE,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
        ResolveRowErrorPolicyTestCase(
            description="project quarantine leaves a sibling-alias model on fail silently",
            model_contents=(
                "MODEL (); SELECT kafka_key::String AS a, (a || 'x')::String AS b, "
                '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
            ),
            project_policy=RowErrorPolicy.QUARANTINE,
            pipeline_policy=None,
            pipeline_mode=PipelineMode.DIRECT,
            expected_policy=RowErrorPolicy.FAIL,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_row_error_settings_when_compiling_then_policy_resolves(
    test_case: ResolveRowErrorPolicyTestCase,
    tmp_path: Path,
) -> None:
    model: CompiledTableModel = build_row_error_model(
        tmp_path=tmp_path,
        source_contents=_SOURCE_CONTENTS,
        model_contents=test_case.model_contents,
        project_policy=test_case.project_policy,
        pipeline_policy=test_case.pipeline_policy,
        pipeline_mode=test_case.pipeline_mode,
    )

    assert model.on_row_error is test_case.expected_policy


@pytest.mark.parametrize(
    "test_case",
    [
        RejectRowErrorPolicyTestCase(
            description="group by",
            model_contents=f"MODEL (on_row_error quarantine); {_GROUPED_SELECT}",
            pipeline_mode=PipelineMode.DIRECT,
            expected_error_fragment="GROUP BY",
        ),
        RejectRowErrorPolicyTestCase(
            description="window function",
            model_contents=f"MODEL (on_row_error quarantine); {_WINDOW_SELECT}",
            pipeline_mode=PipelineMode.DIRECT,
            expected_error_fragment="window function",
        ),
        RejectRowErrorPolicyTestCase(
            description="sibling alias names the column and reason",
            model_contents=(
                "MODEL (on_row_error quarantine); SELECT kafka_key::String AS a, "
                "(a || 'x')::String AS b, "
                '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
            ),
            pipeline_mode=PipelineMode.DIRECT,
            expected_error_fragment="column 'b' is ineligible.*'a'",
        ),
        RejectRowErrorPolicyTestCase(
            description="dynamic column names the column and reason",
            model_contents=(
                "MODEL (on_row_error quarantine); SELECT kafka_key::Dynamic AS a, "
                '_replay_timestamp::DateTime64(3) AS _replay_timestamp FROM __source("orders")'
            ),
            pipeline_mode=PipelineMode.DIRECT,
            expected_error_fragment="column 'a' is ineligible.*Dynamic",
        ),
        RejectRowErrorPolicyTestCase(
            description="virtual pipeline",
            model_contents=f"MODEL (on_row_error quarantine); {_ROW_LEVEL_SELECT}",
            pipeline_mode=PipelineMode.VIRTUAL,
            expected_error_fragment="direct-mode",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_quarantine_on_ineligible_model_when_compiling_then_it_errors(
    test_case: RejectRowErrorPolicyTestCase,
    tmp_path: Path,
) -> None:
    with pytest.raises(PipelineCompileError, match=test_case.expected_error_fragment):
        build_row_error_model(
            tmp_path=tmp_path,
            source_contents=_SOURCE_CONTENTS,
            model_contents=test_case.model_contents,
            project_policy=None,
            pipeline_mode=test_case.pipeline_mode,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        RowErrorHeaderRejectedTestCase(
            description="unknown value",
            model_contents=f"MODEL (on_row_error skip); {_ROW_LEVEL_SELECT}",
            expected_error_fragment="must define 'on_row_error'",
        ),
        RowErrorHeaderRejectedTestCase(
            description="view model",
            model_contents="MODEL (kind view, on_row_error quarantine); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: on_row_error",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_row_error_header_when_loading_then_it_is_rejected(
    test_case: RowErrorHeaderRejectedTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_pipeline_project(
        project_dir=tmp_path,
        source_contents=_SOURCE_CONTENTS,
        model_contents=test_case.model_contents,
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        load_pipeline_directory(pipeline_dir)


@pytest.mark.parametrize(
    "test_case",
    [
        PipelineRowErrorPolicyTestCase(
            description="pipeline.toml sets quarantine",
            pipeline_toml='[defaults.models]\non_row_error = "quarantine"\n',
            expected_policy=RowErrorPolicy.QUARANTINE,
        ),
        PipelineRowErrorPolicyTestCase(
            description="pipeline.toml leaves it unset",
            pipeline_toml="",
            expected_policy=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_toml_when_loading_then_row_error_policy_is_read(
    test_case: PipelineRowErrorPolicyTestCase,
    tmp_path: Path,
) -> None:
    pipeline_dir: Path = write_registry_pipeline_project(
        project_dir=tmp_path,
        source_contents=_SOURCE_CONTENTS,
        model_contents=f"MODEL (); {_ROW_LEVEL_SELECT}",
    )
    (pipeline_dir / "pipeline.toml").write_text(test_case.pipeline_toml, encoding="utf-8")

    assert (
        load_pipeline_directory(pipeline_dir).pipeline.model_on_row_error
        is test_case.expected_policy
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
