"""Build the stable identity of one compiled SQL test."""

import sqltide._native as _native
from sqltide.compiler.quality.models import QualityNodeIdentity
from sqltide.compiler.quality.types import QualityNodeKind
from sqltide.compiler.sql_analysis.main._canonicalize_sql import canonicalize_sql
from sqltide.compiler.test_discovery.models import LoadedSqlTest
from sqltide.compiler.testing.models import SqlTestCase


def build_test_quality_identity(
    *, loaded_test: LoadedSqlTest, test_case: SqlTestCase, dialect: str
) -> QualityNodeIdentity:
    """Build authored-definition and assembled-execution identity for one SQL test."""

    node_name: str = loaded_test.name or loaded_test.file_path.stem
    binding_key, definition_fingerprint, execution_fingerprint = _native.quality_test_identity(
        node_name,
        str(loaded_test.mode),
        [assertion.name for assertion in test_case.assertion_cases],
        [target.target_model_name for target in test_case.target_cases],
        [
            (cte.name, canonicalize_sql(sql=cte.query, dialect=dialect))
            for cte in loaded_test.authored_ctes
        ],
        canonicalize_sql(sql=test_case.query, dialect=dialect),
    )
    return QualityNodeIdentity(
        node_kind=QualityNodeKind.TEST,
        node_name=node_name,
        binding_key=binding_key,
        definition_fingerprint=definition_fingerprint,
        execution_fingerprint=execution_fingerprint,
    )
