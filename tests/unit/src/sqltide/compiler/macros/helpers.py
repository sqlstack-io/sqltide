from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import sqltide._native as _native
from sqltide.compiler.discovery.models import DiscoveredProjectFile
from sqltide.compiler.macros.main._build_macro_context import build_macro_context
from sqltide.compiler.macros.main._expand_macro_calls import expand_macro_calls
from sqltide.compiler.macros.main._load_declarations import load_declarations
from sqltide.compiler.macros.main._load_macro_registry import load_macro_registry
from sqltide.compiler.macros.models import MacroContext, MacroRegistry


def build_test_macro_runtime(project_dir: Path) -> tuple[MacroRegistry, MacroContext]:
    macro_files: tuple[DiscoveredProjectFile, ...] = tuple(
        DiscoveredProjectFile(
            file_path=macro_path,
            relative_path=macro_path.relative_to(project_dir),
            contents=macro_path.read_text(encoding="utf-8"),
        )
        for macro_path in sorted((project_dir / "macros").rglob("*.py"))
    )
    registry: MacroRegistry = load_macro_registry(macro_files=macro_files)
    context: MacroContext = build_macro_context(
        adapter_name="clickhouse",
        dialect="clickhouse",
        target_name="dev",
        database="analytics",
        schema="analytics_schema",
        virtual_environments=False,
        variables={"nested": {"value": "configured"}},
    )
    return registry, context


def expand_project_sql_macros(*, project_dir: Path, sql: str, file_path: Path) -> str:
    registry: MacroRegistry
    context: MacroContext
    registry, context = build_test_macro_runtime(project_dir)
    return expand_macro_calls(
        sql=sql,
        file_path=file_path,
        registry=registry,
        context=context,
    )


def write_project_file(project_dir: Path) -> Path:
    project_file_path: Path = project_dir / "sqltide_project.toml"
    project_file_path.parent.mkdir(parents=True, exist_ok=True)
    project_file_path.write_text(
        'name = "test_project"\ndefault_target = "dev"\n\n[targets.dev]\ndatabase = "analytics"\n',
        encoding="utf-8",
    )
    return project_file_path


def write_macro_file(project_dir: Path, relative_path: str, contents: str) -> Path:
    macro_file_path: Path = project_dir / "macros" / relative_path
    macro_file_path.parent.mkdir(parents=True, exist_ok=True)
    macro_file_path.write_text(dedent(contents).strip() + "\n", encoding="utf-8")
    return macro_file_path


def write_sql_file(project_dir: Path, relative_path: str, contents: str) -> Path:
    sql_file_path: Path = project_dir / relative_path
    sql_file_path.parent.mkdir(parents=True, exist_ok=True)
    sql_file_path.write_text(dedent(contents).strip() + "\n", encoding="utf-8")
    return sql_file_path


DECLARED_CONSTANTS: str = """
CONSTANT (name statuses, value ["paid", "shipped"]);
CONSTANT (name codes, value {3, 1, 2}, render_as array);
CONSTANT (name threshold, type decimal, value "2500.00");
CONSTANT (name label, value "it's");
CONSTANT (name settings, value (retries 3, mode "fast"));
CONSTANT (name amount_column, value "amount");
"""
DECLARED_ENUMS: str = """
ENUM (name order_status, members [PAID, SHIPPED]);
ENUM (name priority, members (LOW 1, HIGH 2));
"""


def expand_with_declarations(
    project_dir: Path, sql: str, *, constants: str | None = None, enums: str | None = None
) -> str:
    write_project_file(project_dir)
    write_macro_file(
        project_dir,
        "common.py",
        """
        def doubled(column: str) -> str:
            return f"{column} * 2"
        """,
    )
    registry: MacroRegistry
    context: MacroContext
    registry, context = build_test_macro_runtime(project_dir)
    declarations: _native.Declarations = load_declarations(
        constant_files=(
            _declaration_file(project_dir, "constants/main.sql", constants or DECLARED_CONSTANTS),
        ),
        enum_files=(_declaration_file(project_dir, "enums/main.sql", enums or DECLARED_ENUMS),),
    )
    return expand_macro_calls(
        sql=sql,
        file_path=project_dir / "pipelines/orders/order_items.sql",
        registry=replace(registry, declarations=declarations),
        context=context,
    )


def _declaration_file(
    project_dir: Path, relative_path: str, contents: str
) -> DiscoveredProjectFile:
    return DiscoveredProjectFile(
        file_path=project_dir / relative_path,
        relative_path=Path(relative_path),
        contents=contents,
    )
