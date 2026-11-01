"""Vocabulary for authored SQL-native tests."""

from enum import StrEnum


class SqlTestMode(StrEnum):
    """Supported authored SQL-native test modes."""

    MODEL = "model"
    MACRO = "macro"


type NativeSqlTestBlock = tuple[int, tuple[str, str, int] | None]
