"""Invocation-scoped logical relation rewriting."""

import sqltide._native as _native


class SqlReferenceRewriter:
    """Rewrite references with one private Polyglot relation cache per invocation."""

    def __init__(self, *, dialect: str) -> None:
        self._analyzer: _native.SqlTreeAnalyzer = _native.SqlTreeAnalyzer(dialect)

    def rewrite(self, *, sql: str, resolver: dict[str, str]) -> str:
        """Replace logical relation calls while preserving authored surrounding text."""

        return self._analyzer.rewrite_references(sql, resolver)
