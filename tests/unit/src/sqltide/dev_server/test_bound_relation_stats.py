import pytest

from sqltide._native import apply_bound_relation_stats
from tests.unit.src.sqltide.dev_server._test_types import BoundRelationStatsTestCase

_STAGED: str = "tbl__orders__20260410T005500Z_cd34ef"
_ACTIVE: str = "tbl__orders__20260408T091200Z_a1b2cd"


@pytest.mark.parametrize(
    "test_case",
    [
        BoundRelationStatsTestCase(
            description="a bound logical view reports the relation it points at",
            stats=(("tbl__orders", 0, 0, 0), (_ACTIVE, 1200, 5120, 3)),
            bindings=(("tbl__orders", _ACTIVE),),
            expected_rows_by_relation=(("tbl__orders", 1200), (_ACTIVE, 1200)),
            expected_parts_by_relation=(("tbl__orders", 3),),
        ),
        BoundRelationStatsTestCase(
            description="an unbound relation keeps its own measurement",
            stats=(("tbl__orders", 900, 2048, 2),),
            bindings=(),
            expected_rows_by_relation=(("tbl__orders", 900),),
            expected_parts_by_relation=(("tbl__orders", 2),),
        ),
        BoundRelationStatsTestCase(
            description="a binding whose relation is absent leaves the view untouched",
            stats=(("tbl__orders", 0, 0, 0),),
            bindings=(("tbl__orders", _STAGED),),
            expected_rows_by_relation=(("tbl__orders", 0),),
            expected_parts_by_relation=(("tbl__orders", 0),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_stable_bindings_when_measuring_then_views_report_their_backing_relation(
    test_case: BoundRelationStatsTestCase,
) -> None:
    stats: dict[str, dict[str, int]] = {
        name: {"rows": rows, "bytes": byte_count, "parts": parts}
        for name, rows, byte_count, parts in test_case.stats
    }

    resolved: dict[str, dict[str, int]] = apply_bound_relation_stats(
        stats=stats, bindings=test_case.bindings
    )

    assert (
        tuple((name, resolved[name]["rows"]) for name, _ in test_case.expected_rows_by_relation)
        == test_case.expected_rows_by_relation
    )
    assert (
        tuple((name, resolved[name]["parts"]) for name, _ in test_case.expected_parts_by_relation)
        == test_case.expected_parts_by_relation
    )


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
