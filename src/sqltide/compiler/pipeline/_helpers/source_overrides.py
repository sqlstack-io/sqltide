"""In-memory authored source replacement for compiler-verified formatting candidates."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from sqltide.compiler.discovery.models import DiscoveredProjectInputs


def override_sources(
    *, inputs: DiscoveredProjectInputs, overrides: Mapping[Path, str]
) -> DiscoveredProjectInputs:
    """Retain discovery ownership while replacing model, test and audit source text."""

    return replace(
        inputs,
        model_files=tuple(
            replace(source, contents=overrides.get(source.file_path, source.contents))
            for source in inputs.model_files
        ),
        test_files=tuple(
            replace(source, contents=overrides.get(source.file_path, source.contents))
            for source in inputs.test_files
        ),
        audit_files=tuple(
            replace(source, contents=overrides.get(source.file_path, source.contents))
            for source in inputs.audit_files
        ),
    )
