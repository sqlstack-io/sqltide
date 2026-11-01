"""Layout-only SQLClean formatting with opaque template bytes and all native safety guards."""

import json
from typing import Any

import sqltide._native as _native
from sqltide.compiler.discovery.models import DiscoveredProjectFile
from sqltide.compiler.rules._helpers.bodies import authored_bodies, protected_sql
from sqltide.compiler.rules.constants import CRLF, LF
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import LintBody


def formatted_file(*, source: DiscoveredProjectFile, keyword: str, dialect: str) -> str:
    """Format only the body ranges, retaining headers and surrounding whitespace exactly."""

    bodies: tuple[LintBody, ...] = authored_bodies(source=source, keyword=keyword)
    if not bodies:
        raise SqlRulesError(f"{source.file_path}: expected a {keyword} header")
    candidate: str = source.contents
    for body in reversed(bodies):
        candidate = (
            candidate[: body.start]
            + formatted_body(body=body, dialect=dialect)
            + candidate[body.end :]
        )
    return candidate


def formatted_body(*, body: LintBody, dialect: str) -> str:
    """Keep templates opaque while SQLClean validates tokens, comments and idempotence."""

    sql, replacements, _ = protected_sql(sql=body.sql)
    response: dict[str, Any] = json.loads(
        _native.sqlclean_format(
            json.dumps(
                {
                    "version": 1,
                    "sql": sql,
                    "dialect": dialect,
                    "token_widths": {
                        token: len(original) for token, original in replacements.items()
                    },
                }
            )
        )
    )
    if not response["formatted"]:
        raise SqlRulesError(
            f"{body.path}: formatter declined: {response.get('reason', 'unknown reason')}"
        )
    formatted: str = response["sql"].rstrip()
    if CRLF in body.contents:
        formatted = formatted.replace(CRLF, LF).replace(LF, CRLF)
    for token, original in replacements.items():
        if formatted.count(token) != 1:
            raise SqlRulesError(f"{body.path}: formatter did not preserve an opaque template")
        formatted = formatted.replace(token, original)
    return formatted
