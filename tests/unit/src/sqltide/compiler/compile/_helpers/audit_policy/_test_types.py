from dataclasses import dataclass


@dataclass(frozen=True)
class ValidateSqlAuditsErrorTestCase:
    description: str
    project_files: tuple[tuple[str, str], ...]
    expected_error_fragment: str


@dataclass(frozen=True)
class ValidateSqlAuditsTestCase:
    description: str
    project_files: tuple[tuple[str, str], ...]
    expected_referenced_model_names: tuple[str, ...]
    expected_severity: str
    expected_name: str | None = None
