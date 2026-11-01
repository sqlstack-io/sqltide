from pathlib import Path
from typing import cast

import pytest

from sqltide.compiler.discovery._helpers.model_sql import (
    infer_transform_source,
    load_transform_from_sql_file,
    parse_model_sql,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError
from sqltide.compiler.discovery.models import TransformStep, ViewStep
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from tests.unit.src.sqltide.compiler.discovery._helpers._test_types import (
    InferTransformSourceErrorTestCase,
    LoadModelDescriptionTestCase,
    LoadModelExecutionSettingsTestCase,
    LoadModelKindTestCase,
    LoadTransformFromSqlFileTestCase,
    ParseModelSqlHeaderErrorTestCase,
    ParseModelSqlHeaderTestCase,
)
from tests.unit.src.sqltide.compiler.macros.helpers import (
    build_test_macro_runtime,
    write_macro_file,
    write_project_file,
    write_sql_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderTestCase(
            description="accepts an empty model header",
            contents="""
        MODEL ();

        SELECT kafka_key::String AS order_id FROM __source("orders")
        """,
            expected_header_values={},
            expected_query='SELECT kafka_key::String AS order_id FROM __source("orders")',
        ),
        ParseModelSqlHeaderTestCase(
            description="accepts blank lines inside multiline replay on change header",
            contents="""
        MODEL (
          engine "MergeTree()",

          order_by ["order_id"],
          replay_anchor never,

          replay_on_change (
            breaking bounded-8s

            non_breaking bounded-8s,
          ),
        );

        SELECT kafka_key::String AS order_id FROM __source("orders")
        """,
            expected_header_values={
                "engine": "MergeTree()",
                "order_by": ["order_id"],
                "replay_anchor": "never",
                "replay_on_change": {
                    "breaking": "bounded-8s",
                    "non_breaking": "bounded-8s",
                },
            },
            expected_query='SELECT kafka_key::String AS order_id FROM __source("orders")',
        ),
        ParseModelSqlHeaderTestCase(
            description="accepts nested and inline SQLBuild header mappings",
            contents="""
        MODEL (
          engine "MergeTree()",
          order_by ["order_id"],
          settings (
            index_granularity 8192
            allow_nullable_key 1,
          ),
          replay_on_change (breaking full, non_breaking bounded-30m),
        );

        SELECT kafka_key::String AS order_id FROM __source("orders")
        """,
            expected_header_values={
                "engine": "MergeTree()",
                "order_by": ["order_id"],
                "settings": {
                    "index_granularity": 8192,
                    "allow_nullable_key": 1,
                },
                "replay_on_change": {
                    "breaking": "full",
                    "non_breaking": "bounded-30m",
                },
            },
            expected_query='SELECT kafka_key::String AS order_id FROM __source("orders")',
        ),
        ParseModelSqlHeaderTestCase(
            description="accepts whitespace-only lines and indented header entries",
            contents=(
                "   \n"
                'MODEL (\n    engine "MergeTree()",\n  \n'
                '    order_by ["order_id"],\n'
                "    bounded_replay_fallback bounded_without_history,\n"
                ");\n \n"
                'SELECT kafka_key::String AS order_id FROM __source("orders")'
            ),
            expected_header_values={
                "engine": "MergeTree()",
                "order_by": ["order_id"],
                "bounded_replay_fallback": "bounded_without_history",
            },
            expected_query='SELECT kafka_key::String AS order_id FROM __source("orders")',
        ),
        ParseModelSqlHeaderTestCase(
            description="accepts replay-scoped execution settings",
            contents="""
        MODEL (
          execution_settings (
            replay (max_threads 8, max_block_size 32),
          ),
        );
        SELECT kafka_key::String AS order_id FROM __source("orders")
        """,
            expected_header_values={
                "execution_settings": {"replay": {"max_threads": 8, "max_block_size": 32}}
            },
            expected_query='SELECT kafka_key::String AS order_id FROM __source("orders")',
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_model_header_variants_when_parsing_then_it_returns_expected_header_values(
    test_case: ParseModelSqlHeaderTestCase,
) -> None:
    header_values, query = parse_model_sql(
        contents=test_case.contents, file_path=Path("orders.sql")
    )

    assert header_values == test_case.expected_header_values
    assert query == test_case.expected_query


@pytest.mark.parametrize(
    "test_case",
    [
        LoadModelKindTestCase(
            description="defaults an empty MODEL header to a table step",
            contents='MODEL (); SELECT 1::UInt8 AS value FROM __source("orders")',
            expected_step_type=TransformStep,
            expected_relation_name=None,
            expected_has_engine=True,
        ),
        LoadModelKindTestCase(
            description="loads a zero-upstream view without table fields",
            contents="MODEL (kind view, relation_name exact_view); SELECT 1::UInt8 AS value",
            expected_step_type=ViewStep,
            expected_relation_name="exact_view",
            expected_has_engine=False,
        ),
        LoadModelKindTestCase(
            description="loads a view with arbitrary source and model upstreams",
            contents=(
                'MODEL (kind view); SELECT 1::UInt8 AS value FROM __source("orders") '
                'JOIN __source("payments") ON 1 = 1 JOIN __ref("customers") ON 1 = 1'
            ),
            expected_step_type=ViewStep,
            expected_relation_name=None,
            expected_has_engine=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_model_kind_when_loading_sql_then_returns_coherent_step(
    test_case: LoadModelKindTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "model.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    model: TransformStep | ViewStep = load_transform_from_sql_file(file_path=model_path)

    assert isinstance(model, test_case.expected_step_type)
    assert model.relation_name == test_case.expected_relation_name
    assert hasattr(model, "engine") is test_case.expected_has_engine


@pytest.mark.parametrize(
    "test_case",
    [
        LoadModelExecutionSettingsTestCase(
            description="normalizes replay execution settings as immutable strings",
            contents=(
                "MODEL (execution_settings (replay (max_threads 8, max_block_size 32))); "
                'SELECT kafka_key::String AS order_id FROM __source("orders")'
            ),
            expected_replay_settings={"max_threads": "8", "max_block_size": "32"},
        )
    ],
    ids=lambda case: case.description,
)
def test_given_replay_execution_settings_when_loading_model_then_retains_them_by_phase(
    test_case: LoadModelExecutionSettingsTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "model.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    model: TransformStep = cast(TransformStep, load_transform_from_sql_file(file_path=model_path))

    assert dict(model.execution_settings.replay or {}) == test_case.expected_replay_settings


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderErrorTestCase(
            description="rejects the removed YAML-like key separator",
            contents='MODEL (engine: "MergeTree()"); SELECT 1::UInt8 AS value',
            expected_error_fragment=(
                "unexpected ':' after key 'engine'; use SQLBuild syntax 'engine value'"
            ),
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects a nested YAML-like brace mapping",
            contents=("MODEL (settings {index_granularity: 8192}); SELECT 1::UInt8 AS value"),
            expected_error_fragment=(
                "YAML-like brace mappings are not supported at position 9; "
                "use SQLBuild parenthesized mapping syntax"
            ),
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects a top-level YAML-like brace mapping",
            contents='MODEL ({engine: "MergeTree()"}); SELECT 1::UInt8 AS value',
            expected_error_fragment=(
                "YAML-like brace mappings are not supported at position 0; "
                "use SQLBuild parenthesized mapping syntax"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_yaml_like_model_header_when_parsing_then_it_raises_conversion_guidance(
    test_case: ParseModelSqlHeaderErrorTestCase,
) -> None:
    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        parse_model_sql(contents=test_case.contents, file_path=Path("orders.sql"))


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderErrorTestCase(
            description="rejects unsupported execution phases",
            contents=(
                "MODEL (execution_settings (audit (max_threads 8))); "
                'SELECT kafka_key::String AS order_id FROM __source("orders")'
            ),
            expected_error_fragment="unsupported execution_settings keys: audit",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unsupported_execution_phase_when_loading_model_then_it_fails_before_compile(
    test_case: ParseModelSqlHeaderErrorTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "orders.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        load_transform_from_sql_file(file_path=model_path)


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderErrorTestCase(
            description="rejects a list replay anchor",
            contents=(
                "MODEL (replay_anchor [auto]); "
                'SELECT kafka_key::String AS order_id FROM __source("orders")'
            ),
            expected_error_fragment="must define 'replay_anchor' as 'auto' or 'never'",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects a mapping bounded replay fallback",
            contents=(
                "MODEL (bounded_replay_fallback (mode fail)); "
                'SELECT kafka_key::String AS order_id FROM __source("orders")'
            ),
            expected_error_fragment="unsupported bounded_replay_fallback",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_non_scalar_replay_choice_when_loading_model_then_it_raises_discovery_error(
    test_case: ParseModelSqlHeaderErrorTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "orders.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        load_transform_from_sql_file(file_path=model_path)


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderErrorTestCase(
            description="rejects engine on a view",
            contents='MODEL (kind view, engine "MergeTree()"); SELECT 1::UInt8 AS value',
            expected_error_fragment="cannot define table/replay fields: engine",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects order by on a view",
            contents="MODEL (kind view, order_by [value]); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: order_by",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects partition by on a view",
            contents="MODEL (kind view, partition_by value); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: partition_by",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects ttl on a view",
            contents="MODEL (kind view, ttl value); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: ttl",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects settings on a view",
            contents="MODEL (kind view, settings (value 1)); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: settings",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects replay anchor on a view",
            contents="MODEL (kind view, replay_anchor never); SELECT 1::UInt8 AS value",
            expected_error_fragment="cannot define table/replay fields: replay_anchor",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects replay on change on a view",
            contents=(
                "MODEL (kind view, replay_on_change (breaking full)); SELECT 1::UInt8 AS value"
            ),
            expected_error_fragment="cannot define table/replay fields: replay_on_change",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects bounded replay fallback on a view",
            contents=("MODEL (kind view, bounded_replay_fallback full); SELECT 1::UInt8 AS value"),
            expected_error_fragment="cannot define table/replay fields: bounded_replay_fallback",
        ),
        ParseModelSqlHeaderErrorTestCase(
            description="rejects ref type annotations on a view",
            contents=(
                "MODEL (kind view); SELECT 1::UInt8 AS value FROM "
                '__ref("orders", ref_type="reference")'
            ),
            expected_error_fragment="cannot declare ref_type annotations for: orders",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_view_with_table_semantics_when_loading_then_rejects_field(
    test_case: ParseModelSqlHeaderErrorTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "view.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        load_transform_from_sql_file(file_path=model_path)


@pytest.mark.parametrize(
    "test_case",
    [
        InferTransformSourceErrorTestCase(
            description="raises a specific error when the driving ref declares ref_type",
            query='SELECT order_id FROM __ref("orders", ref_type="reference")',
            expected_error_fragment="must not declare ref_type for its driving input 'orders'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_typed_driving_ref_when_inferring_transform_source_then_it_raises_specific_error(
    test_case: InferTransformSourceErrorTestCase,
) -> None:
    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        infer_transform_source(
            query=test_case.query,
            file_path=Path("orders.sql"),
            source_line=1,
            source_column=1,
        )


@pytest.mark.parametrize(
    "test_case",
    [
        LoadTransformFromSqlFileTestCase(
            description="expands project macros in the sql model body only",
            macro_file_contents="""
            def replay_columns() -> str:
                return "_replay_partition AS _replay_partition"
            """,
            model_file_contents="""
            MODEL (
              order_by ["order_id"]
            );

            SELECT order_id, @replay_columns() FROM __source("orders")
            """,
            expected_query_fragment=(
                'SELECT order_id, _replay_partition AS _replay_partition FROM __source("orders")'
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sql_model_macros_when_loading_then_it_expands_query_body(
    test_case: LoadTransformFromSqlFileTestCase,
    tmp_path: Path,
) -> None:
    write_project_file(tmp_path)
    write_macro_file(tmp_path, "common_columns.py", test_case.macro_file_contents)
    model_file_path: Path = write_sql_file(
        tmp_path,
        "pipelines/orders/order_items.sql",
        test_case.model_file_contents,
    )
    macro_registry: MacroRegistry
    macro_context: MacroContext
    macro_registry, macro_context = build_test_macro_runtime(tmp_path)

    transform: TransformStep = cast(
        TransformStep,
        load_transform_from_sql_file(
            file_path=model_file_path,
            macro_registry=macro_registry,
            macro_context=macro_context,
        ),
    )

    assert transform.query is not None
    assert test_case.expected_query_fragment in transform.query


@pytest.mark.parametrize(
    "test_case",
    [
        LoadModelDescriptionTestCase(
            description="loads a table model description from the header",
            contents=(
                'MODEL (description "Order fact table."); '
                'SELECT 1::UInt8 AS value FROM __source("orders")'
            ),
            expected_description="Order fact table.",
        ),
        LoadModelDescriptionTestCase(
            description="loads a view model description from the header",
            contents='MODEL (kind view, description "Terminal rollup."); SELECT 1::UInt8 AS value',
            expected_description="Terminal rollup.",
        ),
        LoadModelDescriptionTestCase(
            description="defaults an absent description to none",
            contents='MODEL (); SELECT 1::UInt8 AS value FROM __source("orders")',
            expected_description=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_model_header_when_loading_sql_then_returns_expected_description(
    test_case: LoadModelDescriptionTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "model.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    model: TransformStep | ViewStep = load_transform_from_sql_file(file_path=model_path)

    assert model.description == test_case.expected_description


@pytest.mark.parametrize(
    "test_case",
    [
        ParseModelSqlHeaderErrorTestCase(
            description="rejects a non-string description",
            contents='MODEL (description 7); SELECT 1::UInt8 AS value FROM __source("orders")',
            expected_error_fragment="must define 'description' as a string when set",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_description_when_loading_sql_then_it_raises_specific_error(
    test_case: ParseModelSqlHeaderErrorTestCase,
    tmp_path: Path,
) -> None:
    model_path: Path = tmp_path / "model.sql"
    model_path.write_text(test_case.contents, encoding="utf-8")

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        load_transform_from_sql_file(file_path=model_path)
