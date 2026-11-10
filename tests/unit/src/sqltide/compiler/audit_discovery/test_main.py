from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.discovery.models import TransformStep, ViewStep
from sqltide.compiler.macros.models import MacroContext, MacroRegistry
from tests.unit.src.sqltide.compiler.audit_discovery._test_types import (
    DiscoverGenericSqlAuditsErrorTestCase,
    DiscoverGenericSqlAuditsTestCase,
    DiscoverSqlAuditPolicyTestCase,
    DiscoverSqlAuditsErrorTestCase,
    DiscoverSqlAuditsTestCase,
    DiscoverSqlAuditsWithMacrosTestCase,
)
from tests.unit.src.sqltide.compiler.audit_discovery.helpers import (
    write_model_sql_file,
    write_sql_audit_file,
)
from tests.unit.src.sqltide.compiler.macros.helpers import (
    build_test_macro_runtime,
    write_macro_file,
    write_project_file,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlAuditsTestCase(
            description="discovers a minimal audit with default severity",
            relative_file_path="order_events/no_negative_line_totals.sql",
            file_contents="""
        AUDIT ();

        SELECT order_id
        FROM __ref("order_items")
        WHERE line_total < 0
        """,
            expected_audit_names=(None,),
            expected_referenced_model_names=(("order_items",),),
            expected_severities=("error",),
            expected_descriptions=(None,),
        ),
        DiscoverSqlAuditsTestCase(
            description="discovers multiple named audits from one file",
            relative_file_path="singular/order_events/quality.sql",
            file_contents="""
        AUDIT (name "negative totals", severity warning);

        SELECT order_id
        FROM __ref("order_items")
        WHERE line_total < 0;

        AUDIT (name "missing orders");

        SELECT oi.order_id
        FROM __ref("order_items") AS oi
        LEFT JOIN __ref("orders") AS o ON oi.order_id = o.order_id
        WHERE o.order_id IS NULL
        """,
            expected_audit_names=("negative totals", "missing orders"),
            expected_referenced_model_names=(("order_items",), ("order_items", "orders")),
            expected_severities=("warning", "error"),
            expected_descriptions=(None, None),
        ),
        DiscoverSqlAuditsTestCase(
            description="ignores audit marker lookalikes inside strings and comments",
            relative_file_path="order_events/marker_literals.sql",
            file_contents="""
        AUDIT ();

        SELECT 'marker
        AUDIT (name: fake);
        still marker' AS marker
        -- AUDIT (name: ignored);
        FROM __ref("order_items")
        """,
            expected_audit_names=(None,),
            expected_referenced_model_names=(("order_items",),),
            expected_severities=("error",),
            expected_descriptions=(None,),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_valid_sql_audit_files_when_discovering_then_it_returns_loaded_sql_audits(
    test_case: DiscoverSqlAuditsTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    write_sql_audit_file(audits_root / test_case.relative_file_path, test_case.file_contents)

    loaded_audits: list[LoadedSqlAudit] = _native.discover_sql_audits(root=audits_root)

    assert (
        tuple(loaded_audit.name for loaded_audit in loaded_audits) == test_case.expected_audit_names
    )
    assert (
        tuple(loaded_audit.referenced_model_names for loaded_audit in loaded_audits)
        == test_case.expected_referenced_model_names
    )
    assert (
        tuple(loaded_audit.severity for loaded_audit in loaded_audits)
        == test_case.expected_severities
    )
    assert (
        tuple(loaded_audit.description for loaded_audit in loaded_audits)
        == test_case.expected_descriptions
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlAuditPolicyTestCase(
            description="parses explicit audit policy into typed overrides",
            header=(
                'name "orders are valid", severity warning, every "2m", '
                'warmup "15m", scheduled true'
            ),
            expected_severity_explicit=True,
            expected_cadence_seconds=120,
            expected_warmup_seconds=900,
            expected_scheduled_override=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_audit_policy_when_discovering_then_it_parses_typed_overrides(
    test_case: DiscoverSqlAuditPolicyTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    write_sql_audit_file(
        audits_root / "orders.sql",
        f'AUDIT ({test_case.header}); SELECT * FROM __ref("orders")',
    )

    audit: LoadedSqlAudit = _native.discover_sql_audits(root=audits_root)[0]

    assert audit.severity_is_explicit is test_case.expected_severity_explicit
    assert audit.cadence_seconds_override == test_case.expected_cadence_seconds
    assert audit.warmup_seconds_override == test_case.expected_warmup_seconds
    assert audit.scheduled_override is test_case.expected_scheduled_override


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlAuditsErrorTestCase(
            description="rejects audits without refs",
            relative_file_path="order_events/no_refs.sql",
            file_contents="""
        AUDIT ();

        SELECT 1
        """,
            expected_error_fragment="must reference at least one model",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects unsupported severity",
            relative_file_path="order_events/bad_severity.sql",
            file_contents="""
        AUDIT (severity info);

        SELECT * FROM __ref("order_items")
        """,
            expected_error_fragment="must define severity as 'error' or 'warning'",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects source refs",
            relative_file_path="order_events/source_ref.sql",
            file_contents="""
        AUDIT ();

        SELECT * FROM __source("orders")
        """,
            expected_error_fragment=r"__source\(\.\.\.\) is not allowed",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects unnamed multi audit files",
            relative_file_path="singular/order_events/unnamed_multi.sql",
            file_contents="""
        AUDIT ();
        SELECT * FROM __ref("order_items");

        AUDIT (name "named");
        SELECT * FROM __ref("orders")
        """,
            expected_error_fragment=(
                r"contains multiple AUDIT\(\.\.\.\) "
                r"blocks; each must define name"
            ),
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects malformed audit SQL through Polyglot",
            relative_file_path="order_events/malformed.sql",
            file_contents="""
        AUDIT ();
        SELECT * FROM __ref("order_items") WHERE (
        """,
            expected_error_fragment="exactly one valid top-level query",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects multiple audit SQL statements",
            relative_file_path="order_events/multiple.sql",
            file_contents="""
        AUDIT ();
        SELECT * FROM __ref("order_items"); SELECT 2
        """,
            expected_error_fragment="exactly one valid top-level query",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects non-query audit statements",
            relative_file_path="order_events/delete.sql",
            file_contents="""
        AUDIT ();
        DELETE FROM __ref("order_items") WHERE order_id = 1
        """,
            expected_error_fragment="SELECT or set-operation query",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects zero audit cadence",
            relative_file_path="order_events/zero_cadence.sql",
            file_contents='AUDIT (every "0s"); SELECT * FROM __ref("order_items")',
            expected_error_fragment="every must be greater than zero",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects contradictory schedule opt out",
            relative_file_path="order_events/schedule_conflict.sql",
            file_contents=(
                'AUDIT (scheduled false, every "5m"); SELECT * FROM __ref("order_items")'
            ),
            expected_error_fragment="cannot define both scheduled false and every",
        ),
        DiscoverSqlAuditsErrorTestCase(
            description="rejects removed colon header syntax",
            relative_file_path="order_events/legacy_header.sql",
            file_contents='AUDIT (severity: warning); SELECT * FROM __ref("order_items")',
            expected_error_fragment="unexpected ':' after key 'severity'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_sql_audit_files_when_discovering_then_it_raises_clear_errors(
    test_case: DiscoverSqlAuditsErrorTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    write_sql_audit_file(audits_root / test_case.relative_file_path, test_case.file_contents)

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        _native.discover_sql_audits(root=audits_root)


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverSqlAuditsWithMacrosTestCase(
            description="expands project macros in audit bodies",
            macro_file_contents="""
            def negative_predicate() -> str:
                return "line_total < 0"
            """,
            audit_file_contents="""
            AUDIT ();

            SELECT order_id
            FROM __ref("order_items")
            WHERE @negative_predicate()
            """,
            expected_query_fragment="line_total < 0",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sql_audit_macros_when_discovering_then_it_expands_audit_body(
    test_case: DiscoverSqlAuditsWithMacrosTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    write_project_file(tmp_path)
    write_macro_file(tmp_path, "audit_helpers.py", test_case.macro_file_contents)
    write_sql_audit_file(
        audits_root / "order_events/macro_audit.sql", test_case.audit_file_contents
    )
    macro_registry: MacroRegistry
    macro_context: MacroContext
    macro_registry, macro_context = build_test_macro_runtime(tmp_path)

    loaded_audits: list[LoadedSqlAudit] = _native.discover_sql_audits(
        root=audits_root,
        macro_registry=macro_registry,
        macro_context=macro_context,
    )

    assert test_case.expected_query_fragment in loaded_audits[0].query


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverGenericSqlAuditsTestCase(
            description="renders generic sql audits from schema attached instances",
            definition_name="not_null",
            definition_file_contents="""
            AUDIT ();

            SELECT @column
            FROM __ref("@model")
            WHERE @column IS NULL
            """,
            model_file_contents="""
            MODEL (
              columns (
                order_id (
                    audits [not_null (
                      name "order items order id not null",
                      severity warning,
                      every "2m",
                      warmup "10m",
                      scheduled true,
                    )],
                ),
              ),
            );
            SELECT order_id FROM __source("order_events")
            """,
            expected_name="order items order id not null",
            expected_query_fragments=('FROM __ref("order_items")',),
            expected_referenced_model_names=("order_items",),
            expected_cadence_seconds=120,
            expected_warmup_seconds=600,
            expected_scheduled_override=True,
        ),
        DiscoverGenericSqlAuditsTestCase(
            description="renders escaped quoted generic audit argument lists",
            definition_name="accepted_values",
            definition_file_contents="""
            AUDIT ();

            SELECT @column
            FROM __ref("@model")
            WHERE @column NOT IN (@'values')
            """,
            model_file_contents="""
            MODEL (
              columns (
                category (
                  audits [
                    accepted_values (name "accepted categories", values ["O'Reilly", "cafe雪"]),
                  ],
                ),
              ),
            );
            SELECT category FROM __source("order_events")
            """,
            expected_name="accepted categories",
            expected_query_fragments=(
                "NOT IN ('O''Reilly', 'cafe雪')",
                'FROM __ref("order_items")',
            ),
            expected_referenced_model_names=("order_items",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_generic_sql_audits_when_discovering_then_it_renders_concrete_audits(
    test_case: DiscoverGenericSqlAuditsTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    pipeline_dir: Path = tmp_path / "pipelines" / "order_events"
    pipeline_dir.mkdir(parents=True)
    write_sql_audit_file(
        audits_root / "generic" / f"{test_case.definition_name}.sql",
        test_case.definition_file_contents,
    )
    model_path: Path = pipeline_dir / "order_items.sql"
    write_model_sql_file(model_path, test_case.model_file_contents)
    model: TransformStep | ViewStep = _native.load_transform_from_sql_file(file_path=model_path)

    loaded_audits: list[LoadedSqlAudit] = _native.discover_sql_audits(
        root=audits_root,
        generic_audit_instances=_native.build_model_audit_instances(models=(model,)),
    )

    assert loaded_audits[0].name == test_case.expected_name
    assert all(
        fragment in loaded_audits[0].query for fragment in test_case.expected_query_fragments
    )
    assert loaded_audits[0].referenced_model_names == test_case.expected_referenced_model_names
    assert loaded_audits[0].cadence_seconds_override == test_case.expected_cadence_seconds
    assert loaded_audits[0].warmup_seconds_override == test_case.expected_warmup_seconds
    assert loaded_audits[0].scheduled_override is test_case.expected_scheduled_override


@pytest.mark.parametrize(
    "test_case",
    [
        DiscoverGenericSqlAuditsErrorTestCase(
            description="rejects content before the generic audit header",
            definition_file_contents="""
            SELECT 1;
            AUDIT ();
            SELECT @column FROM __ref("@model")
            """,
            expected_error_fragment="must not contain content before the AUDIT",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_generic_sql_audit_when_discovering_then_it_raises_clear_error(
    test_case: DiscoverGenericSqlAuditsErrorTestCase,
    tmp_path: Path,
) -> None:
    audits_root: Path = tmp_path / "audits"
    write_sql_audit_file(
        audits_root / "generic" / "invalid.sql",
        test_case.definition_file_contents,
    )

    with pytest.raises(ValueError, match=test_case.expected_error_fragment):
        _native.discover_sql_audits(root=audits_root)
