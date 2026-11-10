"""Public entrypoint for authored projection-name inference."""

import sqltide._native as _native


def infer_projection_names(*, sql: str, dialect: str) -> tuple[str, ...]:
    """Return the output column names produced by one authored query."""

    return _native.sql_infer_projection_names(sql, dialect)
