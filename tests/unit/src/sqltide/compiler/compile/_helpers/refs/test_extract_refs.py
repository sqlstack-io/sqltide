import pytest

import sqltide._native as _native
from sqltide.compiler.compile.models import ParsedRef
from tests.unit.src.sqltide.compiler.compile._helpers.refs._test_types import (
    ExtractRefsErrorTestCase,
    ExtractRefsTestCase,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ExtractRefsTestCase(
            description="extracts source and ref calls from plain and nested query positions",
            sql=(
                'SELECT * FROM __source("orders") WHERE customer_id IN '
                '(SELECT customer_id FROM __ref("customers", ref_type="reference"))'
            ),
            expected_refs=(("orders", None), ("customers", "reference")),
        ),
        ExtractRefsTestCase(
            description="ignores source text inside string literals",
            sql='SELECT \'__source("orders")\' AS label FROM __source("orders")',
            expected_refs=(("orders", None),),
        ),
        ExtractRefsTestCase(
            description="extracts ref_type from additional refs",
            sql=(
                'SELECT * FROM __source("orders") LEFT JOIN '
                '__ref("customer_tier", ref_type="reference") USING customer_id'
            ),
            expected_refs=(("orders", None), ("customer_tier", "reference")),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_when_extracting_refs_then_it_returns_only_real_ref_calls(
    test_case: ExtractRefsTestCase,
) -> None:
    extracted_refs: list[ParsedRef] = _native.extract_refs(sql=test_case.sql)

    assert (
        tuple((parsed_ref.name, parsed_ref.ref_type) for parsed_ref in extracted_refs)
        == test_case.expected_refs
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ExtractRefsErrorTestCase(
            description="raises value error when ref has too many arguments",
            sql='SELECT * FROM __ref("orders", "customers")',
            expected_error_type=ValueError,
            expected_error_fragment="optional second argument must be ref_type",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_ref_with_too_many_args_when_extracting_then_it_raises_expected_error(
    test_case: ExtractRefsErrorTestCase,
) -> None:
    with pytest.raises(test_case.expected_error_type, match=test_case.expected_error_fragment):
        _native.extract_refs(sql=test_case.sql)


@pytest.mark.parametrize(
    "test_case",
    [
        ExtractRefsErrorTestCase(
            description="raises value error when ref argument is not string like",
            sql="SELECT * FROM __source(1)",
            expected_error_type=ValueError,
            expected_error_fragment="quoted string or identifier",
        ),
        ExtractRefsErrorTestCase(
            description="raises value error when ref keyword is not ref_type",
            sql='SELECT * FROM __ref("orders", kind="mutable")',
            expected_error_type=ValueError,
            expected_error_fragment="must use the ref_type keyword",
        ),
        ExtractRefsErrorTestCase(
            description="raises value error when ref_type value is invalid",
            sql='SELECT * FROM __ref("orders", ref_type="weird")',
            expected_error_type=ValueError,
            expected_error_fragment="ref_type value must be 'reference' or 'mutable'",
        ),
        ExtractRefsErrorTestCase(
            description="raises value error when source declares ref_type",
            sql='SELECT * FROM __source("orders", ref_type="reference")',
            expected_error_type=ValueError,
            expected_error_fragment="must not declare ref_type",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_ref_with_non_string_arg_when_extracting_then_it_raises_expected_error(
    test_case: ExtractRefsErrorTestCase,
) -> None:
    with pytest.raises(test_case.expected_error_type, match=test_case.expected_error_fragment):
        _native.extract_refs(sql=test_case.sql)
