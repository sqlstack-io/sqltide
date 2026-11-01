"""Produce formatted source candidates without writing or opening a connection."""

from pathlib import Path

from sqltide.compiler.compile.models import CompilerAdapterProfile
from sqltide.compiler.discovery.main._discover_project_inputs import discover_project_inputs
from sqltide.compiler.discovery.models import DiscoveredProjectInputs, LoadedProject
from sqltide.compiler.pipeline.main.analyze_project import analyze_project
from sqltide.compiler.rules._helpers.configuration import rules_config
from sqltide.compiler.rules._helpers.fixes import fixed_file
from sqltide.compiler.rules._helpers.formatting import formatted_file
from sqltide.compiler.rules.models import FormattedSource, RulesConfig


def format_project_sql(
    *,
    root: Path,
    loaded_project: LoadedProject | None,
    adapter_profile: CompilerAdapterProfile,
    selection: tuple[str, ...] | None,
    paths: tuple[Path, ...],
    fix: bool,
) -> tuple[FormattedSource, ...]:
    """Keep file ownership and fix eligibility inside the compiler's SQL boundary."""

    config: RulesConfig = rules_config(loaded_project=loaded_project, selection=selection)
    if any(entry.selectors for entry in config.rule_ignores):
        _ = analyze_project(
            pipelines_root=root,
            loaded_project=loaded_project,
            adapter_profile=adapter_profile,
            enforce_rules=False,
            rule_selection=selection,
        )
    discovered: DiscoveredProjectInputs = discover_project_inputs(
        pipelines_root=root, loaded_project=loaded_project
    )
    results: list[FormattedSource] = []
    for files, keyword in (
        (discovered.model_files, "MODEL"),
        (discovered.test_files, "TEST"),
        (discovered.audit_files, "AUDIT"),
    ):
        for source in files:
            if paths and not any(
                source.file_path.resolve() == path
                or source.file_path.resolve().is_relative_to(path)
                for path in paths
            ):
                continue
            try:
                candidate, diagnostics = (
                    fixed_file(
                        source=source,
                        keyword=keyword,
                        root=root,
                        loaded_project=loaded_project,
                        adapter_profile=adapter_profile,
                        selection=selection,
                    )
                    if fix
                    else (
                        formatted_file(
                            source=source,
                            keyword=keyword,
                            dialect=adapter_profile.sql_analysis_dialect,
                        ),
                        (),
                    )
                )
                results.append(
                    FormattedSource(
                        path=source.file_path,
                        original=source.contents,
                        formatted=candidate,
                        diagnostics=diagnostics,
                    )
                )
            except ValueError as error:
                results.append(
                    FormattedSource(
                        path=source.file_path,
                        original=source.contents,
                        formatted=source.contents,
                        error=str(error),
                    )
                )
    return tuple(results)
