"""SQLAlchemy engine glue for SQLTide's relational control store."""

import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


class RelationalDriver:
    """A pooled SQLAlchemy engine for one SQLite or PostgreSQL URL."""

    def __init__(self, *, url: str) -> None:
        sqlite_url: bool = url.startswith("sqlite:")
        self._engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if sqlite_url else {},
            pool_pre_ping=True,
        )
        if sqlite_url:
            event.listen(self._engine, "connect", _enable_sqlite_foreign_keys)

    def begin(self) -> Connection:
        """Open one pooled connection with its transaction begun."""

        connection: Connection = self._engine.connect()
        connection.begin()
        return connection

    @staticmethod
    def execute(
        *, connection: Connection, sql: str, parameters: Mapping[str, object]
    ) -> tuple[tuple[dict[str, object], ...], int]:
        """Run one statement with bound parameters; return its rows and affected row count."""

        result: CursorResult[Any] = connection.execute(text(sql), dict(parameters))
        rows: tuple[dict[str, object], ...] = (
            tuple(dict(row) for row in result.mappings()) if result.returns_rows else ()
        )
        return rows, result.rowcount

    @staticmethod
    def commit(connection: Connection) -> None:
        try:
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def rollback(connection: Connection) -> None:
        try:
            connection.rollback()
        finally:
            connection.close()

    @staticmethod
    def error_kind(error: BaseException) -> str | None:
        """Classify an exception as `integrity`, another `driver` failure, or neither."""

        if isinstance(error, IntegrityError):
            return "integrity"
        if isinstance(error, SQLAlchemyError):
            return "driver"
        return None

    def close(self) -> None:
        self._engine.dispose()


def _enable_sqlite_foreign_keys(*arguments: object) -> None:
    dbapi_connection: sqlite3.Connection = cast(sqlite3.Connection, arguments[0])
    cursor: sqlite3.Cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
