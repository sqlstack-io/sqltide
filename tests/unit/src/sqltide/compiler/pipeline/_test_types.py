from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayExecutionSettingsTestCase:
    description: str
    expected_settings: dict[str, str]


@dataclass(frozen=True)
class AnalyzeProjectTestCase:
    description: str
    expected_pipeline_names: tuple[str, ...]
    expected_graph_names: tuple[str, ...]
    expected_adapter_name: str
    expected_dialect: str
    expected_default_database: str
    expected_source_file_count: int
    expected_phase_call_count: int
    expected_test_case_count: int
    expected_assembly_realization_order: tuple[str, ...]
    expected_logical_source_count: int
    expected_logical_model_count: int
    expected_source_resource_counts: tuple[int, ...]
    expected_model_resource_counts: tuple[int, ...]
    expected_macro_names: tuple[str, ...]
    expected_macro_target_name: str
    expected_macro_virtual_environments: bool
    expected_macro_variables: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class DestructionTargetMetadataTestCase:
    description: str
    initial_limit: str
    fresh_limit: str
    expected_initial_bytes: int
    expected_fresh_bytes: int


@dataclass(frozen=True)
class AnalysisDialectTestCase:
    description: str
    dialect: str
    expected_error_fragment: str


@dataclass(frozen=True)
class CompiledAuditPolicyTestCase:
    description: str
    audit_header: str
    expected_severity: str
    expected_cadence_seconds: int | None
    expected_warmup_seconds: int
    expected_scheduled: bool


@dataclass(frozen=True)
class ProjectSqlAnalysisCallCountTestCase:
    description: str
    expected_model_count: int
    expected_analyze_calls: int


@dataclass(frozen=True)
class DuplicateProjectInputTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class CompilationEntrypointsTestCase:
    description: str
    expected_entrypoint_paths: tuple[str, ...]
    expected_entrypoint_count: int
    expected_assembly_call_count: int
    expected_realization_call_count: int
    expected_consumer_rebuild_count: int


@dataclass(frozen=True)
class ReadOnceCompilationTestCase:
    description: str
    expected_relative_source_paths: tuple[str, ...]
    expected_exit_code: int
    expected_macro_loader_read_count: int
    expected_macro_import_count: int
    expected_macro_names: tuple[str, ...]
    expected_macro_relative_path: str
    expected_macro_source_fragment: str


@dataclass(frozen=True)
class SharedMacroRuntimeTestCase:
    description: str
    expected_expansion_call_count: int
    expected_registry_identity: bool
    expected_context_identity: bool


@dataclass(frozen=True)
class PrivateMacroDiscoveryTestCase:
    description: str
    expected_macro_names: tuple[str, ...]


@dataclass(frozen=True)
class ReplayPolicyModeErrorTestCase:
    description: str
    project_contents: str
    local_contents: str
    pipeline_config_contents: str
    model_contents: str
    expected_error_fragment: str


@dataclass(frozen=True)
class SharedSourceRealizationTestCase:
    description: str
    expected_logical_source_count: int
    expected_model_count: int
    expected_source_resource_count: int
    expected_consumer_group: str
    expected_effective_consumer_group: str


@dataclass(frozen=True)
class ManagedSourceTtlPrecedenceTestCase:
    description: str
    project_default_ttl: str
    source_ttl_declaration: str
    expected_landing_ttl: str


@dataclass(frozen=True)
class ManagedSourceRetentionTestCase:
    description: str
    source_retention_declaration: str
    expected_landing_ttl: str | None
