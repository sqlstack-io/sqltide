"""Native shapes keep the frozen dataclass contract of the classes they replaced."""

import dataclasses
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from sqltide.compiler.audit_discovery.models import LoadedSqlAudit
from sqltide.compiler.graph.models import ProjectGraph
from sqltide.compiler.rules.exceptions import RulesError
from sqltide.compiler.sql_analysis.exceptions import (
    SqlAnalysisError,
    SqlStorageReferenceError,
)
from sqltide.compiler.sql_analysis.models import (
    SqlCatalogAnalysis,
    SqlNamedQuery,
    SqlReference,
    SqlRelationIdentity,
    SqlRelationRewrite,
    SqlSourceSpan,
)
from sqltide.compiler.test_discovery.models import SqlTestCte
from tests.unit.src.sqltide.compiler.sql_analysis._test_types import (
    NativeShapeConstructorErrorTestCase,
    NativeShapeExceptionTestCase,
    NativeShapeIdentityTestCase,
    NativeShapeNormalizationTestCase,
    NativeShapeProtocolTestCase,
)

_SPAN: SqlSourceSpan = SqlSourceSpan(start=0, end=6, line=1, column=1, end_line=1, end_column=7)


@pytest.mark.parametrize(
    "test_case",
    [
        NativeShapeConstructorErrorTestCase(
            description="names every missing required field",
            args=(),
            kwargs={},
            expected_message=(
                "SqlRelationRewrite.__init__() missing 2 required positional arguments: "
                "'source_name' and 'target_relation'"
            ),
        ),
        NativeShapeConstructorErrorTestCase(
            description="names one missing required field",
            args=("orders",),
            kwargs={},
            expected_message=(
                "SqlRelationRewrite.__init__() missing 1 required positional argument: "
                "'target_relation'"
            ),
        ),
        NativeShapeConstructorErrorTestCase(
            description="counts the accepted positional range when defaults exist",
            args=("orders", "db.orders", (None,), True, "extra"),
            kwargs={},
            expected_message=(
                "SqlRelationRewrite.__init__() takes from 3 to 5 positional arguments "
                "but 6 were given"
            ),
        ),
        NativeShapeConstructorErrorTestCase(
            description="rejects an unknown keyword",
            args=("orders", "db.orders"),
            kwargs={"nosuch": 1},
            expected_message=(
                "SqlRelationRewrite.__init__() got an unexpected keyword argument 'nosuch'"
            ),
        ),
        NativeShapeConstructorErrorTestCase(
            description="rejects a field given positionally and by keyword",
            args=("orders", "db.orders"),
            kwargs={"source_name": "dup"},
            expected_message=(
                "SqlRelationRewrite.__init__() got multiple values for argument 'source_name'"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_arguments_when_constructing_shape_then_error_matches_dataclass(
    test_case: NativeShapeConstructorErrorTestCase,
) -> None:
    with pytest.raises(TypeError) as caught:
        SqlRelationRewrite(*test_case.args, **test_case.kwargs)

    assert str(caught.value) == test_case.expected_message


@pytest.mark.parametrize(
    "test_case",
    [
        NativeShapeProtocolTestCase(
            description="keeps repr, fields, astuple and replace of the frozen dataclass",
            expected_repr=(
                "SqlRelationRewrite(source_name='orders', target_relation='db.orders', "
                "source_databases=('db', None), preserve_source_database=False)"
            ),
            expected_fields=(
                "source_name",
                "target_relation",
                "source_databases",
                "preserve_source_database",
            ),
            expected_astuple=("orders", "db.orders", ("db", None), False),
            expected_replaced_repr=(
                "SqlRelationRewrite(source_name='orders', target_relation='db.orders', "
                "source_databases=('db', None), preserve_source_database=True)"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_native_shape_when_using_dataclass_protocol_then_it_matches_the_dataclass(
    test_case: NativeShapeProtocolTestCase,
) -> None:
    rewrite: SqlRelationRewrite = SqlRelationRewrite(
        "orders", "db.orders", source_databases=("db", None)
    )
    replaced: SqlRelationRewrite = dataclasses.replace(rewrite, preserve_source_database=True)
    query: SqlNamedQuery = SqlNamedQuery(name="recent", query="SELECT 1")

    assert repr(rewrite) == test_case.expected_repr
    assert tuple(field.name for field in dataclasses.fields(rewrite)) == test_case.expected_fields
    assert rewrite.__match_args__ == test_case.expected_fields
    assert dataclasses.astuple(rewrite) == test_case.expected_astuple
    assert hash(rewrite) == hash(test_case.expected_astuple)
    assert rewrite == SqlRelationRewrite("orders", "db.orders", ("db", None), False)
    assert rewrite != replaced
    assert type(replaced) is SqlRelationRewrite
    assert repr(replaced) == test_case.expected_replaced_repr
    assert query != SqlTestCte(name="recent", query="SELECT 1")
    assert query != ("recent", "SELECT 1")
    with pytest.raises(TypeError):
        _ = query < query  # ty: ignore[unsupported-operator]


@pytest.mark.parametrize(
    "test_case",
    [
        NativeShapeNormalizationTestCase(
            description="coerces enums, fills enum defaults and freezes graph mappings",
            relation_type="ref",
            expected_relation_type="<SqlRelationType.REF: 'ref'>",
            expected_invalid_message="'view' is not a valid SqlRelationType",
            expected_attachment_kind="<AuditAttachmentKind.STANDALONE: 'standalone'>",
            expected_edges={"orders": ()},
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_raw_values_when_constructing_shape_then_it_normalizes_like_post_init(
    test_case: NativeShapeNormalizationTestCase,
) -> None:
    reference: SqlReference = SqlReference(
        name="orders", relation_type=test_case.relation_type, span=_SPAN
    )
    audit: LoadedSqlAudit = LoadedSqlAudit(
        file_path=Path("a.sql"), query="SELECT 1", referenced_model_names=()
    )
    edges: dict[str, tuple[object, ...]] = dict(test_case.expected_edges)
    graph: ProjectGraph = ProjectGraph(
        project=cast(Any, None),
        upstream_edges_by_key=cast(Any, edges),
        downstream_edges_by_key={},
        ordered_keys=(),
    )
    edges["late"] = ()

    assert repr(reference.relation_type) == test_case.expected_relation_type
    assert dataclasses.replace(reference, relation_type="source").relation_type == "source"
    with pytest.raises(ValueError, match=test_case.expected_invalid_message):
        SqlReference(name="orders", relation_type="view", span=_SPAN)
    assert repr(audit.attachment_kind) == test_case.expected_attachment_kind
    assert isinstance(graph.upstream_edges_by_key, MappingProxyType)
    assert dict(graph.upstream_edges_by_key) == test_case.expected_edges


@pytest.mark.parametrize(
    "test_case",
    [
        NativeShapeIdentityTestCase(
            description="refuses assignment and names missing attributes like the dataclass",
            missing_attribute="nmae",
            expected_frozen_message="cannot assign to field 'name'",
            expected_missing_message="'SqlRelationIdentity' object has no attribute 'nmae'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_native_shape_when_assigning_or_reading_missing_field_then_errors_match(
    test_case: NativeShapeIdentityTestCase,
) -> None:
    first: SqlRelationIdentity = SqlRelationIdentity(database="db", name="orders")
    analysis: SqlCatalogAnalysis = SqlCatalogAnalysis(
        canonical_sql="",
        query_sql=None,
        source_relations=(first, SqlRelationIdentity(database=None, name="other")),
        direct_source=None,
        target_relation=None,
        engine=None,
        ttl=None,
        settings=(),
    )

    with pytest.raises(dataclasses.FrozenInstanceError, match=test_case.expected_frozen_message):
        first.name = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(AttributeError) as caught:
        getattr(first, test_case.missing_attribute)
    assert str(caught.value) == test_case.expected_missing_message
    assert (caught.value.name, caught.value.obj) == (test_case.missing_attribute, first)
    assert analysis.first_source is first
    assert dataclasses.replace(analysis, source_relations=()).first_source is None


@pytest.mark.parametrize(
    "test_case",
    [
        NativeShapeExceptionTestCase(
            description="keeps exception messages, arguments, bases and attributes",
            unknown_column_names=("missing", "gone"),
            expected_args=("Unknown model output columns in ttl: missing, gone",),
            expected_repr=(
                "SqlStorageReferenceError('Unknown model output columns in ttl: missing, gone')"
            ),
            expected_rules_notice_prefix="\nSQLClean default SQL Rules are enabled.",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_native_exception_when_raised_then_it_keeps_its_python_contract(
    test_case: NativeShapeExceptionTestCase,
) -> None:
    error: SqlStorageReferenceError = SqlStorageReferenceError(
        kind="ttl",
        expression="missing + INTERVAL 1 DAY",
        unknown_column_names=test_case.unknown_column_names,
        available_column_names=("event_at",),
    )
    notice: RulesError = RulesError(findings=(), defaults=True)

    assert isinstance(error, SqlAnalysisError)
    assert isinstance(error, ValueError)
    assert error.args == test_case.expected_args
    assert repr(error) == test_case.expected_repr
    assert (error.span, error.unknown_column_names) == (None, test_case.unknown_column_names)
    assert notice.findings == ()
    assert str(notice).startswith(test_case.expected_rules_notice_prefix)
    assert str(RulesError(findings=())) == ""
