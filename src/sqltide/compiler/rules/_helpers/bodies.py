"""Adapt retained authored bodies and compiler expansions to SQLClean placeholders."""

from dataclasses import replace

import sqltide._native as _native
from sqltide.compiler.compile.models import CompileProjectInputs
from sqltide.compiler.discovery.models import DiscoveredProjectFile
from sqltide.compiler.rules.constants import MODEL_HEADER, TEST_HEADER
from sqltide.compiler.rules.exceptions import SqlRulesError
from sqltide.compiler.rules.models import LintBody


def lint_bodies(*, inputs: CompileProjectInputs) -> tuple[LintBody, ...]:
    """Reuse expansion results from this compile's effective target; never call macros twice."""

    bodies: list[LintBody] = []
    body: LintBody
    for files, keyword in (
        (inputs.discovered_inputs.model_files, "MODEL"),
        (inputs.discovered_inputs.test_files, "TEST"),
        (inputs.discovered_inputs.audit_files, "AUDIT"),
    ):
        for source in files:
            for body in authored_bodies(source=source, keyword=keyword):
                expanded, spans = inputs.macro_registry.expansions.get(
                    (
                        body.path,
                        body.sql,
                        body.contents.count("\n", 0, body.start) + 1,
                        body.start - body.contents.rfind("\n", 0, body.start),
                    ),
                    (body.sql, []),
                )
                body = replace(body, sql=expanded, passes=(tuple(spans),))
                bodies.append(_neutralized(body=body))
    return tuple(bodies)


def authored_bodies(*, source: DiscoveredProjectFile, keyword: str) -> tuple[LintBody, ...]:
    """Only SQL after extension headers is eligible; headers remain byte-for-byte unchanged."""

    bodies: list[LintBody] = []
    for _, start, header, raw in _native.split_header_blocks(source.contents, keyword):
        fields: dict[str, object] = _native.parse_model_header(header)
        enabled: object = fields.get("sql_analysis", True)
        if type(enabled) is not bool:
            raise SqlRulesError(f"{source.file_path}: sql_analysis must be a boolean")
        leading: int = len(raw) - len(raw.lstrip())
        sql: str = raw.strip()
        bodies.append(
            LintBody(
                path=source.file_path,
                contents=source.contents,
                start=start + leading,
                end=start + leading + len(sql),
                sql=sql,
                model=keyword == MODEL_HEADER,
                enabled=enabled,
                fixture=keyword == TEST_HEADER,
            )
        )
    return tuple(bodies)


def protected_sql(
    *, sql: str, relation_sites: frozenset[tuple[int, int]] = frozenset()
) -> tuple[str, dict[str, str], tuple[tuple[int, int, int, int], ...]]:
    """Protect exact macro/ref bytes while formatting their surrounding SQL layout."""

    sites: list[tuple[int, int]] = _native.sqlclean_template_sites(sql)
    replacements: dict[str, str] = {}
    spans: list[tuple[int, int, int, int]] = []
    parts: list[str] = []
    cursor: int = 0
    output: int = 0
    prefix: str = "__sqlclean_interpolation_"
    while prefix in sql:
        prefix += "_"
    for index, (start, end) in enumerate(sites):
        token: str = (
            f"__sqlclean_relation_parameter_{index}__"
            if (start, end) in relation_sites
            else f"{prefix}{index}__"
        )
        parts.append(sql[cursor:start])
        output += start - cursor
        parts.append(token)
        spans.append((start, end, output, output + len(token)))
        replacements[token] = sql[start:end]
        output += len(token)
        cursor = end
    parts.append(sql[cursor:])
    return "".join(parts), replacements, tuple(spans)


def source_range(
    *, body: LintBody, start: int, end: int, edit: bool = False
) -> tuple[int, int] | None:
    """Map diagnostics to macro calls, but refuse edits intersecting generated text."""

    for spans in reversed(body.passes):
        shift: int = 0
        mapped_start: int = start
        mapped_end: int = end
        for before_start, before_end, after_start, after_end in spans:
            intersects: bool = start < after_end and end > after_start
            if start == end:
                intersects = after_start <= start < after_end
            if intersects and edit:
                return None
            if after_start <= start < after_end:
                mapped_start = before_start
            elif start >= after_end:
                mapped_start = start + shift + before_end - before_start - (after_end - after_start)
            if after_start < end <= after_end:
                mapped_end = before_end
            elif end >= after_end:
                mapped_end = end + shift + before_end - before_start - (after_end - after_start)
            shift += before_end - before_start - (after_end - after_start)
        start, end = mapped_start, mapped_end
    return body.start + start, body.start + end


def _neutralized(*, body: LintBody) -> LintBody:
    references: list[tuple[str, str, str | None, tuple[int, int, int, int, int, int]]] = (
        _native.extract_references(body.sql)
    )
    prepared, replacements, spans = protected_sql(
        sql=body.sql,
        relation_sites=frozenset(
            (span[0], span[1]) for name, _, _, span in references if name.startswith("@")
        ),
    )
    names: dict[str, str] = {}
    for name, _, _, span in references:
        for token, original in replacements.items():
            if body.sql[span[0] : span[1]] == original:
                names[token] = name
    return replace(
        body,
        sql=prepared,
        passes=(*body.passes, spans),
        dependencies=tuple(names),
        relation_names=names,
        external_identifiers=tuple(token for token in replacements if token not in names),
        macro_outputs=tuple(
            (_forward(position=start, spans=spans), _forward(position=end, spans=spans, right=True))
            for _, _, start, end in body.passes[0]
        ),
    )


def _forward(
    *, position: int, spans: tuple[tuple[int, int, int, int], ...], right: bool = False
) -> int:
    shift: int = 0
    for before_start, before_end, after_start, after_end in spans:
        if position < before_start:
            break
        if position < before_end:
            return after_end if right else after_start
        shift += after_end - after_start - (before_end - before_start)
    return position + shift
