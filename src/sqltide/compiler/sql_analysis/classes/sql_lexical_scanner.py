"""Shared SQL lexical scanning behavior for compiler-owned syntaxes."""

import sqltide._native as _native
from sqltide.compiler.sql_analysis.models import SqlTopLevelCtes


class SqlLexicalScanner:
    """Find syntax outside ClickHouse comments and quoted text."""

    @staticmethod
    def find_matching_parenthesis(*, sql: str, open_index: int, context: str) -> int:
        """Return the close index matching one opening parenthesis."""

        return _native.find_matching_parenthesis(sql, open_index, context)

    @staticmethod
    def find_next_unquoted_character(*, sql: str, character: str, start: int) -> int | None:
        """Find one character outside comments and quoted text."""

        return _native.find_next_unquoted_character(sql, character, start)

    @staticmethod
    def find_next_macro_start(*, sql: str, start: int) -> int | None:
        """Find the next `@name(` macro call outside comments and quoted text."""

        return _native.find_next_macro_start(sql, start)

    @staticmethod
    def skip_trivia(*, sql: str, start: int) -> int:
        """Return the next index that is neither whitespace nor a comment."""

        return _native.skip_trivia(sql, start)

    @staticmethod
    def try_consume_keyword(*, sql: str, start: int, keyword: str) -> int | None:
        """Return the index after one case-insensitive keyword, or None."""

        return _native.try_consume_keyword(sql, start, keyword)

    @staticmethod
    def extract_top_level_ctes(*, sql: str, context: str) -> SqlTopLevelCtes:
        """Return the authored top-level WITH clause and its trailing statement."""

        return _native.extract_top_level_ctes(sql, context)
