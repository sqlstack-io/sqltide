from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide._native import SqlReferenceRewriter, attach_error_diagnostic, extract_references
from sqltide.compiler.compile.exceptions import PipelineCompileError
from sqltide.compiler.compile.models import ParsedRef
from sqltide.compiler.sql_analysis.exceptions import SqlAnalysisError
from sqltide.compiler.sql_analysis.models import SqlReference
from sqltide.diagnostics.models import CompilerDiagnostic
from sqltide.diagnostics.types import DiagnosticPhase
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    CompilerReferenceDiagnosticTestCase,
    ReferenceErrorTestCase,
    ReferenceExtractionTestCase,
    ReferenceParityTestCase,
    RepositoryReferenceFixtureTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ReferenceExtractionTestCase(
            description="extracts source and typed model references with exact spans",
            sql=(
                'SELECT\n* FROM __source("orders") o\n'
                'JOIN __ref("customers", ref_type="mutable") c ON o.id = c.id'
            ),
            expected_references=(
                ("orders", "source", None),
                ("customers", "ref", "mutable"),
            ),
            expected_source_slices=(
                '__source("orders")',
                '__ref("customers", ref_type="mutable")',
            ),
            expected_coordinates=((2, 8, 2, 26), (3, 6, 3, 44)),
        ),
        ReferenceExtractionTestCase(
            description="accepts quoted and bare names plus reference ref type",
            sql=(
                "SELECT * FROM __source(`orders`) "
                "JOIN __ref(customers, ref_type='reference') USING id"
            ),
            expected_references=(
                ("orders", "source", None),
                ("customers", "ref", "reference"),
            ),
            expected_source_slices=(
                "__source(`orders`)",
                "__ref(customers, ref_type='reference')",
            ),
            expected_coordinates=((1, 15, 1, 33), (1, 39, 1, 77)),
        ),
        ReferenceExtractionTestCase(
            description="ignores markers in all quoted forms and SQL comments",
            sql=(
                "SELECT '__ref(\"string_ref\")' AS single_text, "
                '"__source(""double_source"")" AS double_text, '
                "`__ref(``backtick_ref``)` AS backtick_text "
                "FROM __source(orders) -- __ref(line_comment)\n"
                "/* __source(block_comment) */"
            ),
            expected_references=(("orders", "source", None),),
            expected_source_slices=("__source(orders)",),
            expected_coordinates=((1, 140, 1, 156),),
        ),
        ReferenceExtractionTestCase(
            description="handles ClickHouse backslash and doubled quote escapes",
            sql=(
                r"SELECT 'it\'s __ref(escaped)' AS escaped_text, "
                "'it''s __source(doubled)' AS doubled_text "
                "FROM __source(orders)"
            ),
            expected_references=(("orders", "source", None),),
            expected_source_slices=("__source(orders)",),
            expected_coordinates=((1, 95, 1, 111),),
        ),
        ReferenceExtractionTestCase(
            description="accepts comments as reference argument trivia",
            sql=(
                'SELECT * FROM __ref(/* model */ "customers", /* kind */ '
                'ref_type /* equals */ = /* value */ "mutable")'
            ),
            expected_references=(("customers", "ref", "mutable"),),
            expected_source_slices=(
                '__ref(/* model */ "customers", /* kind */ '
                'ref_type /* equals */ = /* value */ "mutable")',
            ),
            expected_coordinates=((1, 15, 1, 103),),
        ),
        ReferenceExtractionTestCase(
            description="requires complete marker identifier boundaries",
            sql=(
                'SELECT * FROM x__ref("prefixed") '
                'JOIN ___ref("underscore") ON 1 = 1 '
                'JOIN __refx("suffixed") ON 1 = 1 '
                'JOIN __ref("valid") ON 1 = 1'
            ),
            expected_references=(("valid", "ref", None),),
            expected_source_slices=('__ref("valid")',),
            expected_coordinates=((1, 107, 1, 121),),
        ),
        ReferenceExtractionTestCase(
            description="preserves duplicate references in authored order",
            sql='SELECT * FROM __ref("orders") o JOIN __ref("orders") p USING id',
            expected_references=(
                ("orders", "ref", None),
                ("orders", "ref", None),
            ),
            expected_source_slices=('__ref("orders")', '__ref("orders")'),
            expected_coordinates=((1, 15, 1, 30), (1, 38, 1, 53)),
        ),
        ReferenceExtractionTestCase(
            description="ignores markers in ClickHouse hash comments",
            sql=(
                'SELECT * FROM __source("orders") # __ref("ignored")\n'
                '#! __source("also_ignored")\nWHERE 1 = 1'
            ),
            expected_references=(("orders", "source", None),),
            expected_source_slices=('__source("orders")',),
            expected_coordinates=((1, 15, 1, 33),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_when_extracting_references_then_returns_typed_source_spans(
    test_case: ReferenceExtractionTestCase,
) -> None:
    references: tuple[SqlReference, ...] = extract_references(test_case.sql)

    assert (
        tuple(
            (reference.name, reference.relation_type, reference.ref_type)
            for reference in references
        )
        == test_case.expected_references
    )
    assert tuple(
        test_case.sql[reference.span.start : reference.span.end] for reference in references
    ) == (test_case.expected_source_slices)
    assert (
        tuple(
            (
                reference.span.line,
                reference.span.column,
                reference.span.end_line,
                reference.span.end_column,
            )
            for reference in references
        )
        == test_case.expected_coordinates
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ReferenceErrorTestCase(
            description="rejects empty arguments",
            sql="SELECT * FROM __ref()",
            expected_error_fragment="arguments must not be empty",
            expected_line=1,
            expected_column=21,
        ),
        ReferenceErrorTestCase(
            description="rejects trailing argument separators",
            sql='SELECT * FROM __ref("orders",)',
            expected_error_fragment="arguments must not be empty",
            expected_line=1,
            expected_column=21,
        ),
        ReferenceErrorTestCase(
            description="rejects source reference types",
            sql='SELECT * FROM __source("orders", ref_type="reference")',
            expected_error_fragment="must not declare ref_type",
            expected_line=1,
            expected_column=15,
        ),
        ReferenceErrorTestCase(
            description="rejects unknown reference type keywords",
            sql='SELECT * FROM __ref("orders", kind="reference")',
            expected_error_fragment="must use the ref_type keyword",
            expected_line=1,
            expected_column=15,
        ),
        ReferenceErrorTestCase(
            description="rejects invalid reference type values",
            sql='SELECT * FROM __ref("orders", ref_type="invalid")',
            expected_error_fragment="value must be 'reference' or 'mutable'",
            expected_line=1,
            expected_column=15,
        ),
        ReferenceErrorTestCase(
            description="rejects empty quoted reference names",
            sql='SELECT * FROM __ref("")',
            expected_error_fragment="must be a quoted string or identifier",
            expected_line=1,
            expected_column=15,
        ),
        ReferenceErrorTestCase(
            description="rejects multiple quoted values in one name argument",
            sql='SELECT * FROM __ref("orders" "customers")',
            expected_error_fragment="must be a quoted string or identifier",
            expected_line=1,
            expected_column=15,
        ),
        ReferenceErrorTestCase(
            description="rejects unclosed quoted text",
            sql='SELECT * FROM __ref("orders)',
            expected_error_fragment="unclosed quoted text",
            expected_line=1,
            expected_column=21,
        ),
        ReferenceErrorTestCase(
            description="rejects unclosed block comments",
            sql="SELECT * FROM __ref(/* orders)",
            expected_error_fragment="unclosed block comment",
            expected_line=1,
            expected_column=21,
        ),
        ReferenceErrorTestCase(
            description="rejects unclosed reference parentheses",
            sql='SELECT * FROM __ref("orders"',
            expected_error_fragment="unclosed parenthesis",
            expected_line=1,
            expected_column=20,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_malformed_reference_when_extracting_then_raises_located_error(
    test_case: ReferenceErrorTestCase,
) -> None:
    with pytest.raises(SqlAnalysisError, match=test_case.expected_error_fragment) as raised:
        extract_references(test_case.sql)

    assert raised.value.span is not None
    assert raised.value.span.line == test_case.expected_line
    assert raised.value.span.column == test_case.expected_column


@pytest.mark.parametrize(
    "test_case",
    [
        ReferenceParityTestCase(
            description="preserves nested source and typed ref facts at the compiler seam",
            sql=(
                'SELECT * FROM __source("orders") o WHERE o.id IN '
                '(SELECT id FROM __ref("customers", ref_type="reference"))'
            ),
            expected_reference_count=2,
        ),
        ReferenceParityTestCase(
            description="preserves duplicate mutable ref occurrences at the compiler seam",
            sql=(
                'SELECT * FROM __ref("orders", ref_type="mutable") o '
                'JOIN __ref("orders", ref_type="mutable") p USING id'
            ),
            expected_reference_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_reference_corpus_when_crossing_compile_seam_then_preserves_analysis_facts(
    test_case: ReferenceParityTestCase,
) -> None:
    analysis_references: tuple[SqlReference, ...] = extract_references(test_case.sql)
    compile_references: list[ParsedRef] = _native.extract_refs(sql=test_case.sql)

    assert len(compile_references) == test_case.expected_reference_count
    assert tuple(
        (reference.name, reference.relation_type, reference.ref_type, reference.span)
        for reference in compile_references
    ) == tuple(
        (reference.name, reference.relation_type, reference.ref_type, reference.span)
        for reference in analysis_references
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompilerReferenceDiagnosticTestCase(
            description="translates query-relative scanner spans to authored model locations",
            sql='SELECT * FROM __ref("orders)',
            source_path="pipelines/orders/orders.sql",
            source_line=8,
            source_column=3,
            expected_location=("pipelines/orders/orders.sql", 8, 23, 8, 31),
            expected_phase="discovery",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_located_model_sql_when_scanning_fails_then_phase_diagnostic_uses_authored_span(
    test_case: CompilerReferenceDiagnosticTestCase,
) -> None:
    with pytest.raises(PipelineCompileError) as raised:
        _native.extract_refs(
            sql=test_case.sql,
            source_path=Path(test_case.source_path),
            source_line=test_case.source_line,
            source_column=test_case.source_column,
        )

    _ = attach_error_diagnostic(
        error=raised.value,
        phase=DiagnosticPhase.DISCOVERY,
        code="SQT-DISCOVERY-001",
        location=None,
    )
    assert isinstance(raised.value.diagnostic, CompilerDiagnostic)
    diagnostic: CompilerDiagnostic = raised.value.diagnostic

    assert diagnostic.location is not None
    assert (
        diagnostic.location.path.as_posix(),
        diagnostic.location.line,
        diagnostic.location.column,
        diagnostic.location.end_line,
        diagnostic.location.end_column,
    ) == test_case.expected_location
    assert diagnostic.phase == test_case.expected_phase


@pytest.mark.parametrize(
    "test_case",
    [
        RepositoryReferenceFixtureTestCase(
            description="keeps every standalone SQL reference fixture in the parity corpus",
            fixture_root="tests/fixtures/sql",
            expected_relative_paths=("at_ref.sql", "underscore_ref.sql"),
            expected_reference_name="web_orders",
            expected_replacement="analytics.web_orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_repository_reference_fixtures_when_analyzing_then_all_parse_through_boundary(
    test_case: RepositoryReferenceFixtureTestCase,
) -> None:
    fixture_root: Path = Path(test_case.fixture_root)
    fixture_paths: tuple[Path, ...] = tuple(sorted(fixture_root.glob("*.sql")))
    fixture_sql: tuple[str, ...] = tuple(path.read_text() for path in fixture_paths)
    extracted_references: tuple[tuple[SqlReference, ...], ...] = tuple(
        extract_references(sql) for sql in fixture_sql
    )
    extracted_names: tuple[str, ...] = tuple(
        references[0].name for references in extracted_references
    )
    rewritten_sql: tuple[str, ...] = tuple(
        SqlReferenceRewriter(dialect="clickhouse").rewrite(
            sql=sql,
            resolver={test_case.expected_reference_name: test_case.expected_replacement},
        )
        for sql in fixture_sql
    )

    assert tuple(path.relative_to(fixture_root).as_posix() for path in fixture_paths) == (
        test_case.expected_relative_paths
    )
    assert tuple(len(references) for references in extracted_references) == tuple(
        1 for _path in fixture_paths
    )
    assert extracted_names == tuple(test_case.expected_reference_name for _path in fixture_paths)
    assert tuple(test_case.expected_replacement in sql for sql in rewritten_sql) == tuple(
        True for _path in fixture_paths
    )
