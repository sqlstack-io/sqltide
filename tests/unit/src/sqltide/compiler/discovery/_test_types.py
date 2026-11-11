from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqltide.compiler.discovery.models import KafkaSettings, SourceFreshnessPolicy
from sqltide.compiler.discovery.types import ModelReferenceScope, PipelineMode

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


@dataclass(frozen=True)
class DiscoverPipelinesTestCase:
    description: str
    pipelines_root: Path
    expected_pipeline_names: list[str]


@dataclass(frozen=True)
class DiscoverPipelinesErrorTestCase:
    description: str
    project_files: dict[str, str]
    expected_error_type: type[Exception]
    expected_error_fragment: str


@dataclass(frozen=True)
class PipelineSourceInferenceTestCase:
    description: str
    project_files: dict[str, str]
    source_contents: str
    expected_pipeline_sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ViewPipelineSourceInferenceTestCase:
    description: str
    project_files: dict[str, str]
    source_contents: str
    expected_pipeline_sources: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class PipelineSourceInferenceErrorTestCase:
    description: str
    project_files: dict[str, str]
    source_contents: str
    expected_error_fragment: str


@dataclass(frozen=True)
class PipelinePrefixViolationTestCase:
    description: str
    pipeline_directory_name: str
    expected_error_fragment: str


@dataclass(frozen=True)
class PipelineNamingMacroTestCase:
    description: str
    pipeline_directory_name: str
    source_name: str
    model_name: str
    macro_source: str
    expected_pipeline_names: list[str]


@dataclass(frozen=True)
class PipelineNamingMacroRenameTestCase:
    description: str
    pipeline_directory_name: str
    macro_source: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ProjectPipelineNamingDefaultTestCase:
    description: str
    expected_pipeline_prefix: str
    expected_pipeline_naming_macro: str | None


@dataclass(frozen=True)
class ProjectPipelineNamingOverrideTestCase:
    description: str
    naming_toml: str
    expected_pipeline_prefix: str
    expected_pipeline_naming_macro: str | None


@dataclass(frozen=True)
class ProjectConfigurationErrorTestCase:
    description: str
    project_contents: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ProjectDependencyScopeTestCase:
    description: str
    dependencies_toml: str
    expected_scope: ModelReferenceScope
    expected_allowed_references: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EffectiveProjectConfigurationTestCase:
    description: str
    expected_name: str
    expected_adapter: str
    expected_target_name: str
    expected_database: str
    expected_pipeline_mode: PipelineMode
    expected_variables: tuple[tuple[str, object], ...]
    expected_connection: tuple[tuple[str, object], ...]
    expected_managed_source_ttl: str
    expected_model_ttl: str
    expected_ui_timezone: str


@dataclass(frozen=True)
class TypedRetentionInterpolationTestCase:
    description: str
    expected_duration_seconds: int
    expected_timestamp_column: str
    expected_cap_at_column: str
    expected_kafka_fallback: str


@dataclass(frozen=True)
class ProjectAuditDefaultsTestCase:
    description: str
    expected_severity: str
    expected_cadence_seconds: int
    expected_warmup_seconds: int


@dataclass(frozen=True)
class ProjectDeploymentReadinessDefaultsTestCase:
    description: str
    expected_maximum_lag_seconds: float
    expected_minimum_staged_row_ratio: float


@dataclass(frozen=True)
class ProjectRunSafetyDefaultTestCase:
    description: str
    expected_seconds: int


@dataclass(frozen=True)
class ProjectBuildLimitResolutionTestCase:
    description: str
    expected_dev_limit: int
    expected_staging_limit: int
    expected_private_limit: int


@dataclass(frozen=True)
class ProjectDestructionLimitResolutionTestCase:
    description: str
    expected_limit_bytes: int


@dataclass(frozen=True)
class SensorEventAgeResolutionTestCase:
    description: str
    expected_dev_seconds: int
    expected_staging_seconds: int


@dataclass(frozen=True)
class LegacyProjectConfigurationTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class SourceRegistryTestCase:
    description: str
    expected_source_names: tuple[str, ...]
    expected_boundary_modes: tuple[str, ...]
    expected_relative_paths: tuple[str, ...]
    expected_managed_source_ttl: str


@dataclass(frozen=True)
class SourceRetentionInterpolationTestCase:
    description: str
    expected_duration_seconds: int
    expected_fallback: str


@dataclass(frozen=True)
class KafkaBrokerDefaultTestCase:
    description: str
    source_broker_yaml: str
    default_broker_list: str
    expected_broker_list: str


@dataclass(frozen=True)
class ProjectKafkaBrokerDefaultTestCase:
    description: str
    configured_broker_list: str
    environment: tuple[tuple[str, str], ...]
    expected_broker_list: str


@dataclass(frozen=True)
class KafkaSourceNamingMacroErrorTestCase:
    description: str
    macro_name: str
    macro_source: str
    sources_yaml: str
    expected_error_fragment: str


@dataclass(frozen=True)
class KafkaSourceNamingMacroSuccessTestCase:
    description: str
    expected_name: str
    expected_topic: str
    expected_origin: str
    expected_macro_name: str | None


@dataclass(frozen=True)
class SourceRegistryErrorTestCase:
    description: str
    source_files: tuple[tuple[str, str], ...]
    expected_error_fragment: str


@dataclass(frozen=True)
class InterpolationSuccessTestCase:
    description: str
    values: tuple[tuple[str, object], ...]
    environment: tuple[tuple[str, str], ...]
    input_value: object
    expected_value: object


@dataclass(frozen=True)
class InterpolationErrorTestCase:
    description: str
    values: tuple[tuple[str, object], ...]
    environment: tuple[tuple[str, str], ...]
    input_value: object
    expected_error_fragment: str


@dataclass(frozen=True)
class MissingProjectConfigurationTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class UnknownTargetTestCase:
    description: str
    selected_target: str
    expected_error_fragment: str


@dataclass(frozen=True)
class LoadedProjectConfigurationTestCase:
    description: str
    expected_name: str
    expected_adapter: str
    expected_default_target: str
    expected_has_local_source: bool
    expected_array: tuple[object, ...]
    expected_mapping: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class MixedProjectConfigurationTestCase:
    description: str
    expected_error_fragment: str


@dataclass(frozen=True)
class LocalConfigurationErrorTestCase:
    description: str
    local_contents: str
    expected_error_fragment: str


@dataclass(frozen=True)
class SourceBoundaryModeTestCase:
    description: str
    source_contents: str
    expected_source_type_name: str
    expected_mode: str
    expected_columns: tuple[str | None, str | None, str | None, str | None, str | None]
    variables: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class SourceFreshnessTestCase:
    description: str
    source_yaml: str
    default_freshness: SourceFreshnessPolicy | None
    expected_freshness: SourceFreshnessPolicy | None


@dataclass(frozen=True)
class ProjectFreshnessDefaultTestCase:
    description: str
    defaults_toml: str
    expected_freshness: SourceFreshnessPolicy | None


@dataclass(frozen=True)
class ProjectFreshnessErrorTestCase:
    description: str
    defaults_toml: str
    expected_error_fragment: str


@dataclass(frozen=True)
class PostgresSourceTestCase:
    description: str
    sources_yaml: str
    expected_host: str
    expected_port: int
    expected_refresh: str
    expected_password_env: str | None
    expected_append: bool


@dataclass(frozen=True)
class PostgresSourceRejectionTestCase:
    description: str
    sources_yaml: str
    expected_error_fragment: str


@dataclass(frozen=True)
class ProjectConnectionSettingsTestCase:
    description: str
    project_contents: str
    expected_settings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProjectProductionTargetTestCase:
    description: str
    expected_production_target: bool


@dataclass(frozen=True)
class ModelStepErrorTestCase:
    description: str
    step_class: type
    arguments: dict[str, object]
    expected_message: str


@dataclass(frozen=True)
class KafkaSettingsReprTestCase:
    description: str
    kafka_settings: KafkaSettings
    expected_repr_pattern: str
    expected_hidden_values: tuple[str, ...]


@dataclass(frozen=True)
class ShallowCopyTestCase:
    description: str
    instance: "DataclassInstance"
    expected_type: type


@dataclass(frozen=True)
class ReplaceTestCase:
    description: str
    instance: "DataclassInstance"
    changes: dict[str, object]
    expected: "DataclassInstance"
