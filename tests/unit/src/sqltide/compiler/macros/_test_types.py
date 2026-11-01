from dataclasses import dataclass


@dataclass(frozen=True)
class ExpandProjectSqlMacrosTestCase:
    description: str
    macro_file_name: str
    macro_file_contents: str
    sql_body: str
    expected_expanded_fragment: str


@dataclass(frozen=True)
class ExpandProjectSqlMacrosErrorTestCase:
    description: str
    macro_file_name: str
    macro_file_contents: str
    sql_body: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ExpandProjectSqlMacrosCollisionTestCase:
    description: str
    first_macro_file_name: str
    first_macro_file_contents: str
    second_macro_file_name: str
    second_macro_file_contents: str
    sql_body: str
    expected_error_fragment: str


@dataclass(frozen=True)
class MacroRuntimeImmutabilityTestCase:
    description: str
    variables: dict[str, object]
    expected_macro_names: tuple[str, ...]
    expected_nested_values: tuple[object, ...]


@dataclass(frozen=True)
class MacroExecutionDiagnosticTestCase:
    description: str
    macro_file_contents: str
    sql_body: str
    expected_error_fragment: str
    expected_sql_line: int
    expected_sql_column: int
    expected_definition_line: int


@dataclass(frozen=True)
class MacroImportDiagnosticTestCase:
    description: str
    macro_file_contents: str
    expected_error_fragment: str
    expected_definition_line: int


@dataclass(frozen=True)
class MacroRegistrationTestCase:
    description: str
    expected_macro_names: tuple[str, ...]
    expected_relative_path: str
    expected_source_fragment: str


@dataclass(frozen=True)
class MacroDescriptionTestCase:
    description: str
    macro_file_contents: str
    macro_name: str
    expected_description: str | None


@dataclass(frozen=True)
class DeclarationReferenceTestCase:
    description: str
    sql_body: str
    expected_sql: str


@dataclass(frozen=True)
class DeclarationReferenceErrorTestCase:
    description: str
    sql_body: str
    expected_error_fragment: str


@dataclass(frozen=True)
class InvalidDeclarationTestCase:
    description: str
    constants: str | None
    enums: str | None
    expected_error_fragment: str
