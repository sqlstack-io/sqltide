"""Shared SQL lexical scanning behavior for compiler-owned syntaxes."""

from collections.abc import Callable

from sqltide.compiler.sql_analysis._helpers.scanning import (
    extract_top_level_ctes_impl,
    find_matching_parenthesis,
    find_next_macro_start,
    find_next_unquoted_character,
    run_native_scan,
    skip_trivia,
    try_consume_keyword_impl,
)
from sqltide.compiler.sql_analysis.models import SqlTopLevelCtes


class SqlLexicalScanner:
    """Find syntax outside ClickHouse comments and quoted text."""

    @staticmethod
    def scanned[T](scan: Callable[[], T]) -> T:
        """Run one native scan, raising its failure as a SQL analysis exception."""

        return run_native_scan(scan)

    @staticmethod
    def find_matching_parenthesis(*, sql: str, open_index: int, context: str) -> int:
        """Return the close index matching one opening parenthesis."""

        return find_matching_parenthesis(
            sql=sql,
            open_index=open_index,
            context=context,
        )

    @staticmethod
    def find_next_unquoted_character(*, sql: str, character: str, start: int) -> int | None:
        """Find one character outside comments and quoted text."""

        return find_next_unquoted_character(
            sql=sql,
            character=character,
            start=start,
        )

    @staticmethod
    def find_next_macro_start(*, sql: str, start: int) -> int | None:
        """Find the next `@name(` macro call outside comments and quoted text."""

        return find_next_macro_start(sql=sql, start=start)

    @staticmethod
    def skip_trivia(*, sql: str, start: int) -> int:
        """Return the next index that is neither whitespace nor a comment."""

        return skip_trivia(sql=sql, start=start)

    @staticmethod
    def try_consume_keyword(*, sql: str, start: int, keyword: str) -> int | None:
        """Return the index after one case-insensitive keyword, or None."""

        return try_consume_keyword_impl(sql=sql, start=start, keyword=keyword)

    @staticmethod
    def extract_top_level_ctes(*, sql: str, context: str) -> SqlTopLevelCtes:
        """Return the authored top-level WITH clause and its trailing statement."""

        return extract_top_level_ctes_impl(sql=sql, context=context)
