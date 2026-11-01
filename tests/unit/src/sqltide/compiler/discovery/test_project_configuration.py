from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

import pytest

from sqltide.compiler.discovery._helpers.configuration import load_project_configuration
from sqltide.compiler.discovery._helpers.effective_configuration import (
    resolve_effective_project_configuration,
)
from sqltide.compiler.discovery._helpers.interpolation import (
    interpolate_config_value,
    resolve_variable_values,
)
from sqltide.compiler.discovery.exceptions import PipelineDiscoveryError, ProjectConfigError
from sqltide.compiler.discovery.models import (
    AuditDefaults,
    DeploymentReadinessDefaults,
    EffectiveProjectConfiguration,
    KafkaRetentionPolicy,
    LoadedProjectConfiguration,
    ModelRetentionPolicy,
    SourceFreshnessPolicy,
)
from sqltide.compiler.discovery.types import ModelReferenceScope, PipelineMode
from tests.unit.src.sqltide.compiler.discovery._test_types import (
    EffectiveProjectConfigurationTestCase,
    InterpolationErrorTestCase,
    InterpolationSuccessTestCase,
    LegacyProjectConfigurationTestCase,
    LoadedProjectConfigurationTestCase,
    LocalConfigurationErrorTestCase,
    MissingProjectConfigurationTestCase,
    MixedProjectConfigurationTestCase,
    ProjectAuditDefaultsTestCase,
    ProjectBuildLimitResolutionTestCase,
    ProjectConfigurationErrorTestCase,
    ProjectConnectionSettingsTestCase,
    ProjectDependencyScopeTestCase,
    ProjectDeploymentReadinessDefaultsTestCase,
    ProjectDestructionLimitResolutionTestCase,
    ProjectFreshnessDefaultTestCase,
    ProjectFreshnessErrorTestCase,
    ProjectPipelineNamingDefaultTestCase,
    ProjectPipelineNamingOverrideTestCase,
    ProjectProductionTargetTestCase,
    ProjectRunSafetyDefaultTestCase,
    SensorEventAgeResolutionTestCase,
    TypedRetentionInterpolationTestCase,
    UnknownTargetTestCase,
)
from tests.unit.src.sqltide.compiler.discovery.helpers import (
    write_legacy_project_file,
    write_local_toml,
    write_project_toml,
)

_FRESHNESS_PROJECT_TOML_TEMPLATE: str = """
name = "analytics"
default_target = "dev"

[connection]
host = "project-host"
port = 8123

{defaults_toml}

[targets.dev]
database = "dev_database"
"""


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectDependencyScopeTestCase(
            description="defaults model references to project scope",
            dependencies_toml="",
            expected_scope=ModelReferenceScope.PROJECT,
        ),
        ProjectDependencyScopeTestCase(
            description="retains committed pipeline scope through effective resolution",
            expected_scope=ModelReferenceScope.PIPELINE,
            expected_allowed_references=(
                ("pl__shared_dimensions", "pl__orders"),
                ("pl__orders", "pl__reporting"),
            ),
            dependencies_toml=(
                '[dependencies]\nmodel_reference_scope = "pipeline"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__shared_dimensions"\n'
                'downstream_pipeline = "pl__orders"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__orders"\n'
                'downstream_pipeline = "pl__reporting"\n'
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_dependency_policy_when_resolving_then_effective_scope_is_retained(
    test_case: ProjectDependencyScopeTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "analytics"\ndefault_target = "dev"\n'
            f"{test_case.dependencies_toml}[targets.dev]\n"
        ),
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="dev",
        cli_variables={},
        environment={},
    )

    assert effective.dependencies.model_reference_scope == test_case.expected_scope
    assert (
        tuple(
            (reference.upstream_pipeline, reference.downstream_pipeline)
            for reference in effective.dependencies.allowed_cross_pipeline_references
        )
        == test_case.expected_allowed_references
    )


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectProductionTargetTestCase(
            description="committed target production status survives effective resolution",
            expected_production_target=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_committed_production_target_when_resolving_then_classification_is_retained(
    test_case: ProjectProductionTargetTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "analytics"\ndefault_target = "live"\n'
            '[targets.live]\ndatabase = "analytics_live"\nproduction = true\n'
        ),
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="live",
        cli_variables={},
        environment={},
    )

    assert effective.production_target is test_case.expected_production_target


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectPipelineNamingDefaultTestCase(
            description="defaults to the pl__ pipeline prefix without a naming macro",
            expected_pipeline_prefix="pl__",
            expected_pipeline_naming_macro=None,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_pipeline_naming_configuration_when_loading_then_default_prefix_is_pl(
    test_case: ProjectPipelineNamingDefaultTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "analytics"\ndefault_target = "dev"\n[targets.dev]\n',
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    assert loaded.project.naming.pipeline_prefix == test_case.expected_pipeline_prefix
    assert loaded.project.naming.pipeline_naming_macro == test_case.expected_pipeline_naming_macro


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectPipelineNamingOverrideTestCase(
            description="retains an empty prefix and a configured naming macro",
            naming_toml='pipeline_prefix = ""\npipeline_naming_macro = "pipeline_name"',
            expected_pipeline_prefix="",
            expected_pipeline_naming_macro="pipeline_name",
        ),
        ProjectPipelineNamingOverrideTestCase(
            description="retains a custom pipeline prefix",
            naming_toml='pipeline_prefix = "custom__"',
            expected_pipeline_prefix="custom__",
            expected_pipeline_naming_macro=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_pipeline_naming_overrides_when_loading_then_values_are_retained(
    test_case: ProjectPipelineNamingOverrideTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=f"""
        name = "analytics"
        default_target = "dev"

        [naming]
        {test_case.naming_toml}

        [targets.dev]
        """,
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    assert loaded.project.naming.pipeline_prefix == test_case.expected_pipeline_prefix
    assert loaded.project.naming.pipeline_naming_macro == test_case.expected_pipeline_naming_macro


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectRunSafetyDefaultTestCase(
            description="parses the project run safety window",
            expected_seconds=90,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_run_safety_default_when_loading_then_duration_is_typed(
    test_case: ProjectRunSafetyDefaultTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [defaults]
        run_presumed_failed_after = "90s"

        [targets.dev]
        database = "analytics"
        """,
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    assert loaded.project.defaults.run_presumed_failed_after_seconds == test_case.expected_seconds


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectBuildLimitResolutionTestCase(
            description="target overrides and local targets inherit the project limit",
            expected_dev_limit=2,
            expected_staging_limit=5,
            expected_private_limit=5,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_and_target_build_limits_when_resolving_then_target_overrides_default(
    test_case: ProjectBuildLimitResolutionTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [build]
        max_pipelines = 5

        [targets.dev]
        database = "dev_database"

        [targets.dev.build]
        max_pipelines = 2

        [targets.staging]
        database = "staging_database"
        """,
    )
    write_local_toml(
        project_dir=tmp_path,
        contents="""
        [targets.private]
        database = "private_database"
        """,
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    dev: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="dev",
        cli_variables={},
        environment={},
    )
    staging: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="staging",
        cli_variables={},
        environment={},
    )
    private: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="private",
        cli_variables={},
        environment={},
    )

    assert dev.build.max_pipelines == test_case.expected_dev_limit
    assert staging.build.max_pipelines == test_case.expected_staging_limit
    assert private.build.max_pipelines == test_case.expected_private_limit


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectDestructionLimitResolutionTestCase(
            description="selected target retains its finite destruction ceiling",
            expected_limit_bytes=107_374_182_400,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_target_destruction_limit_when_resolving_then_bytes_are_target_scoped(
    test_case: ProjectDestructionLimitResolutionTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "uat"

        [targets.uat]
        database = "analytics_uat"

        [targets.uat.destruction]
        max_table_size_to_drop = "100GiB"

        [targets.prod]
        database = "analytics_prod"
        """,
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="uat",
        cli_variables={},
        environment={},
    )
    production: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="prod",
        cli_variables={},
        environment={},
    )

    assert effective.destruction.max_table_size_to_drop_bytes == test_case.expected_limit_bytes
    assert production.destruction.max_table_size_to_drop_bytes is None


@pytest.mark.parametrize(
    "test_case",
    [
        SensorEventAgeResolutionTestCase(
            description="target age overrides the project sensor default",
            expected_dev_seconds=1800,
            expected_staging_seconds=7200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_sensor_event_age_when_resolving_then_target_overrides_project_default(
    test_case: SensorEventAgeResolutionTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [sensors]
        maximum_event_age = "2h"

        [targets.dev]
        database = "dev_database"

        [targets.dev.sensors]
        maximum_event_age = "30m"

        [targets.staging]
        database = "staging_database"
        """,
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    dev: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="dev",
        cli_variables={},
        environment={},
    )
    staging: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="staging",
        cli_variables={},
        environment={},
    )

    assert dev.sensors.maximum_event_age_seconds == test_case.expected_dev_seconds
    assert staging.sensors.maximum_event_age_seconds == test_case.expected_staging_seconds


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectAuditDefaultsTestCase(
            description="parses project audit defaults into typed policy",
            expected_severity="warning",
            expected_cadence_seconds=300,
            expected_warmup_seconds=900,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_audit_defaults_when_loading_then_policy_is_typed(
    test_case: ProjectAuditDefaultsTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [defaults.audits]
        severity = "warning"
        every = "5m"
        warmup = "15m"

        [targets.dev]
        database = "analytics"
        """,
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)
    defaults: AuditDefaults = loaded.project.defaults.audits

    assert defaults.severity == test_case.expected_severity
    assert defaults.cadence_seconds == test_case.expected_cadence_seconds
    assert defaults.warmup_seconds == test_case.expected_warmup_seconds


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectDeploymentReadinessDefaultsTestCase(
            description="parses deployment readiness defaults into typed thresholds",
            expected_maximum_lag_seconds=90.0,
            expected_minimum_staged_row_ratio=0.85,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_readiness_defaults_when_loading_then_thresholds_are_typed(
    test_case: ProjectDeploymentReadinessDefaultsTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [defaults.deployment_readiness]
        maximum_lag = "90s"
        minimum_staged_row_ratio = 0.85

        [targets.dev]
        database = "analytics"
        """,
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)
    defaults: DeploymentReadinessDefaults = loaded.project.defaults.deployment_readiness

    assert defaults.maximum_lag_seconds == test_case.expected_maximum_lag_seconds
    assert defaults.minimum_staged_row_ratio == test_case.expected_minimum_staged_row_ratio


@pytest.mark.parametrize(
    "test_case",
    [
        EffectiveProjectConfigurationTestCase(
            description="local private target and CLI vars resolve through one immutable config",
            expected_name="analytics",
            expected_adapter="clickhouse",
            expected_target_name="private",
            expected_database="cli_database",
            expected_pipeline_mode=PipelineMode.VIRTUAL,
            expected_variables=(
                ("adapter_name", "clickhouse"),
                ("database_name", "cli_database"),
                ("native_count", 7),
                ("region", "local"),
                ("rendered", "local-events"),
                ("target_value", "private"),
                ("ttl_days", 14),
            ),
            expected_connection=(
                ("host", "private-host"),
                ("password", "${ENV:MISSING_PASSWORD}"),
                ("port", 8123),
                ("username", "local-user"),
            ),
            expected_managed_source_ttl="_replay_landed_at + INTERVAL 14 DAY",
            expected_model_ttl="event_at + INTERVAL 14 DAY",
            expected_ui_timezone="Europe/London",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_and_local_toml_when_resolving_then_applies_locked_precedence(
    test_case: EffectiveProjectConfigurationTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        adapter = "project-adapter"
        default_target = "dev"

        [connection]
        host = "project-host"
        port = 8123
        username = "project-user"
        password = "${ENV:MISSING_PASSWORD}"

        [vars]
        region = "project"
        rendered = "${region}-events"
        native_count = 7
        ttl_days = 14

        [defaults]
        pipeline_mode = \"direct\"
        managed_source_ttl = "_replay_landed_at + INTERVAL ${ttl_days} DAY"
        model_ttl = "event_at + INTERVAL ${ttl_days} DAY"

        [ui]
        timezone = "Europe/London"

        [targets.dev]
        database = "dev_database"

        [targets.private]
        database = "${database_name}"

        [targets.private.vars]
        database_name = "project_private_database"
        target_value = "project"

        [targets.private.connection]
        host = "project-private-host"
        """,
    )
    write_local_toml(
        project_dir=tmp_path,
        contents="""
        target = "private"
        adapter = "${adapter_name}"

        [defaults]
        pipeline_mode = \"virtual\"

        [connection]
        username = "local-user"

        [vars]
        adapter_name = "clickhouse"
        region = "local"

        [targets.private]
        database = "${database_name}"

        [targets.private.vars]
        database_name = "local_private_database"
        target_value = "private"

        [targets.private.connection]
        host = "private-host"
        """,
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target=None,
        cli_variables={"database_name": "cli_database"},
        environment={},
    )

    assert effective.name == test_case.expected_name
    assert effective.adapter == test_case.expected_adapter
    assert effective.target_name == test_case.expected_target_name
    assert effective.database == test_case.expected_database
    assert effective.defaults.pipeline_mode == test_case.expected_pipeline_mode
    assert effective.variables == test_case.expected_variables
    assert effective.connection.values == test_case.expected_connection
    assert effective.defaults.managed_source_ttl == test_case.expected_managed_source_ttl
    assert effective.defaults.model_ttl == test_case.expected_model_ttl
    assert effective.ui.timezone == test_case.expected_ui_timezone
    assert loaded.project_source.contents.startswith('name = "analytics"')
    assert loaded.local_source is not None


@pytest.mark.parametrize(
    "test_case",
    [
        TypedRetentionInterpolationTestCase(
            description="typed retention resolves invocation variables and environment",
            expected_duration_seconds=3 * 86_400,
            expected_timestamp_column="kafka_timestamp",
            expected_cap_at_column="ingested_at",
            expected_kafka_fallback="landed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_interpolated_typed_retention_when_resolving_then_policy_uses_precedence(
    test_case: TypedRetentionInterpolationTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents="""
        name = "analytics"
        default_target = "dev"

        [vars]
        retention_duration = "1d"
        model_timestamp = "event_at"

        [defaults.sources.kafka.retention]
        duration = "${retention_duration}"
        timestamp = "broker"
        fallback = "${ENV:RETENTION_FALLBACK}"

        [defaults.models.retention]
        duration = "${retention_duration}"
        timestamp_column = "${model_timestamp}"
        cap_at_column = "ingested_at"

        [targets.dev]

        [targets.dev.vars]
        retention_duration = "2d"
        """,
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target="dev",
        cli_variables={
            "retention_duration": "3d",
            "model_timestamp": "kafka_timestamp",
        },
        environment={"RETENTION_FALLBACK": "landed"},
    )
    model_retention: ModelRetentionPolicy | Literal[False] | None = (
        effective.defaults.models.retention
    )
    kafka_retention: KafkaRetentionPolicy | Literal[False] | None = (
        effective.defaults.sources.kafka.retention
    )

    assert model_retention is not None and model_retention is not False
    assert kafka_retention is not None and kafka_retention is not False
    assert model_retention.duration_seconds == test_case.expected_duration_seconds
    assert model_retention.timestamp_column == test_case.expected_timestamp_column
    assert model_retention.cap_at_column == test_case.expected_cap_at_column
    assert str(kafka_retention.fallback) == test_case.expected_kafka_fallback


@pytest.mark.parametrize(
    "test_case",
    [
        LoadedProjectConfigurationTestCase(
            description="uses immutable defaults when local TOML is absent",
            expected_name="analytics",
            expected_adapter="clickhouse",
            expected_default_target="dev",
            expected_has_local_source=False,
            expected_array=(1, 2),
            expected_mapping=(("key", "value"),),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_absent_local_toml_when_loading_then_returns_typed_defaults(
    test_case: LoadedProjectConfigurationTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=(
            'name = "analytics"\ndefault_target = "dev"\n'
            '[vars]\narray = [1, 2]\nmapping = {key = "value"}\n[targets.dev]\n'
        ),
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    assert loaded.project.name == test_case.expected_name
    assert loaded.project.adapter == test_case.expected_adapter
    assert loaded.project.default_target == test_case.expected_default_target
    assert (loaded.local_source is not None) is test_case.expected_has_local_source
    variables: dict[str, object] = dict(loaded.project.variables)
    assert variables["array"] == test_case.expected_array
    mapping: Mapping[str, object] = cast(Mapping[str, object], variables["mapping"])
    assert tuple(sorted(mapping.items())) == test_case.expected_mapping
    mutable_mapping: dict[str, object] = cast(dict[str, object], mapping)
    with pytest.raises(TypeError):
        mutable_mapping["mutated"] = True


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectConfigurationErrorTestCase(
            description="rejects a removed top-level version key",
            project_contents='name = "analytics"\ndefault_target = "dev"\nversion = 2\n',
            expected_error_fragment="unsupported keys: version",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an invalid UI timezone",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[ui]\ntimezone = "London"\n[targets.dev]\n'
            ),
            expected_error_fragment="ui.timezone must be a valid IANA timezone",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects legacy and typed model retention defaults together",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults]\nmodel_ttl = "event_at + INTERVAL 7 DAY"\n'
                '[defaults.models.retention]\nduration = "7d"\n'
                'timestamp_column = "event_at"\n[targets.dev]\n'
            ),
            expected_error_fragment=(
                "defaults.model_ttl cannot be combined with defaults.models.retention"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects legacy and typed Kafka retention defaults together",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults]\nmanaged_source_ttl = "_replay_landed_at + INTERVAL 7 DAY"\n'
                '[defaults.sources.kafka.retention]\nduration = "7d"\n'
                "[targets.dev]\n"
            ),
            expected_error_fragment=(
                "defaults.managed_source_ttl cannot be combined with "
                "defaults.sources.kafka.retention"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a non-positive typed retention duration",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults.models.retention]\nduration = "0d"\n'
                'timestamp_column = "event_at"\n[targets.dev]\n'
            ),
            expected_error_fragment=(
                "defaults.models.retention.duration must be greater than zero"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a qualified typed retention column",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults.models.retention]\nduration = "7d"\n'
                'timestamp_column = "events.event_at"\n[targets.dev]\n'
            ),
            expected_error_fragment=(
                "defaults.models.retention.timestamp_column must be an unqualified column name"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an invalid model reference scope",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[dependencies]\nmodel_reference_scope = "repository"\n[targets.dev]\n'
            ),
            expected_error_fragment=(
                "dependencies.model_reference_scope must be 'project' or 'pipeline'"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an unknown dependency policy key",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[dependencies]\nunknown = true\n[targets.dev]\n"
            ),
            expected_error_fragment="dependencies contains unsupported keys: unknown",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a non-array cross-pipeline allowlist",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[dependencies]\nallowed_cross_pipeline_references = "pl__orders"\n'
                "[targets.dev]\n"
            ),
            expected_error_fragment="must be an array of tables",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a self cross-pipeline allowlist entry",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__orders"\n'
                'downstream_pipeline = "pl__orders"\n[targets.dev]\n'
            ),
            expected_error_fragment="cannot allow a pipeline to reference itself",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a duplicate directional allowlist entry",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__shared"\n'
                'downstream_pipeline = "pl__orders"\n'
                "[[dependencies.allowed_cross_pipeline_references]]\n"
                'upstream_pipeline = "pl__shared"\n'
                'downstream_pipeline = "pl__orders"\n[targets.dev]\n'
            ),
            expected_error_fragment="duplicates pl__shared -> pl__orders",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a target-level project-wide mode",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev]\ndatabase = "analytics"\npipeline_mode = "virtual"\n'
            ),
            expected_error_fragment="targets.dev contains unsupported keys: pipeline_mode",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a non-positive project build limit",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[build]\nmax_pipelines = 0\n[targets.dev]\n"
            ),
            expected_error_fragment="build.max_pipelines must be a positive integer",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a boolean target build limit",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[build]\nmax_pipelines = 5\n"
                "[targets.dev.build]\nmax_pipelines = true\n"
            ),
            expected_error_fragment=("targets.dev.build.max_pipelines must be a positive integer"),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a numeric target destruction limit",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[targets.dev.destruction]\nmax_table_size_to_drop = 100\n"
            ),
            expected_error_fragment="must be a finite positive byte-size string",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an unlimited target destruction limit",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev.destruction]\nmax_table_size_to_drop = "0GiB"\n'
            ),
            expected_error_fragment="must be a finite positive byte-size string",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a target destruction limit above ClickHouse UInt64",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev.destruction]\nmax_table_size_to_drop = "16EiB"\n'
            ),
            expected_error_fragment="must be a finite positive byte-size string",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a target limit without a project default",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[targets.dev.build]\nmax_pipelines = 2\n"
            ),
            expected_error_fragment=(
                "target build limits require build.max_pipelines as a project default"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects the removed virtual environments setting",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[settings]\nvirtual_environments = true\n[targets.dev]\n"
            ),
            expected_error_fragment="project contains unsupported keys: settings",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an unknown default pipeline mode",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults]\npipeline_mode = "shadow"\n[targets.dev]\n'
            ),
            expected_error_fragment="defaults.pipeline_mode must be 'direct' or 'virtual'",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects the removed bounded parenthesis spelling",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev]\ndatabase = "analytics"\n'
                '[defaults.replay_on_change]\nbreaking = "bounded(7d)"\n'
            ),
            expected_error_fragment="must be 'full' or 'bounded-<duration>'",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects invalid TOML",
            project_contents='name = "analytics\ndefault_target = "dev"',
            expected_error_fragment="contains invalid TOML",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a missing project name",
            project_contents='default_target = "dev"\n[targets.dev]\n',
            expected_error_fragment="must define non-empty string 'name'",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a missing default target",
            project_contents='name = "analytics"\n[targets.dev]\n',
            expected_error_fragment="must define non-empty string 'default_target'",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an unknown project key",
            project_contents='name = "analytics"\ndefault_target = "dev"\nunknown = true',
            expected_error_fragment="unsupported keys: unknown",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects the removed fallback value",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev]\ndatabase = "analytics"\n'
                '[defaults]\nbounded_replay_fallback = "full_refresh"\n'
            ),
            expected_error_fragment="must be 'full' or 'bounded_without_history'",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a non-string managed source TTL default",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[targets.dev]\ndatabase = "analytics"\n'
                "[defaults]\nmanaged_source_ttl = 7\n"
            ),
            expected_error_fragment="defaults.managed_source_ttl must be a non-empty string",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects interpolation in open mapping keys",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[connection]\n"${connection_key}" = "secret"\n[targets.dev]\n'
            ),
            expected_error_fragment="must not interpolate mapping keys",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects interpolation of committed project identity",
            project_contents=('name = "${project_name}"\ndefault_target = "dev"\n[targets.dev]\n'),
            expected_error_fragment="project.name must be a committed literal",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects invalid deployment readiness lag",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults.deployment_readiness]\nmaximum_lag = "soon"\n[targets.dev]\n'
            ),
            expected_error_fragment="defaults.deployment_readiness.maximum_lag must be a duration",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects deployment readiness ratio above one",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[defaults.deployment_readiness]\nminimum_staged_row_ratio = 1.1\n"
                "[targets.dev]\n"
            ),
            expected_error_fragment=(
                "defaults.deployment_readiness.minimum_staged_row_ratio "
                "must be a number from 0 to 1"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects boolean deployment readiness ratio",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[defaults.deployment_readiness]\nminimum_staged_row_ratio = true\n"
                "[targets.dev]\n"
            ),
            expected_error_fragment=(
                "defaults.deployment_readiness.minimum_staged_row_ratio "
                "must be a number from 0 to 1"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects unknown deployment readiness setting",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                "[defaults.deployment_readiness]\nenforced = true\n[targets.dev]\n"
            ),
            expected_error_fragment=(
                "defaults.deployment_readiness contains unsupported keys: enforced"
            ),
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects a run safety window shorter than unresponsive detection",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults]\nrun_presumed_failed_after = "30s"\n[targets.dev]\n'
            ),
            expected_error_fragment="run_presumed_failed_after must be longer than 45s",
        ),
        ProjectConfigurationErrorTestCase(
            description="rejects an array audit severity",
            project_contents=(
                'name = "analytics"\ndefault_target = "dev"\n'
                '[defaults.audits]\nseverity = ["error"]\n[targets.dev]\n'
            ),
            expected_error_fragment="defaults.audits.severity must be 'error' or 'warning'",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_toml_contract_when_loading_then_rejects_with_field_context(
    test_case: ProjectConfigurationErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(project_dir=tmp_path, contents=test_case.project_contents)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        LocalConfigurationErrorTestCase(
            description="rejects local mode at target scope",
            local_contents="""
            [targets.private]
            database = "analytics"
            pipeline_mode = \"direct\"
            """,
            expected_error_fragment=(
                "sqltide_local.toml targets.private contains unsupported keys: pipeline_mode"
            ),
        ),
        LocalConfigurationErrorTestCase(
            description="rejects a local build limit override",
            local_contents="""
            [targets.private.build]
            max_pipelines = 100
            """,
            expected_error_fragment=(
                "sqltide_local.toml targets.private contains unsupported keys: build"
            ),
        ),
        LocalConfigurationErrorTestCase(
            description="rejects local production classification",
            local_contents="""
            [targets.private]
            production = true
            """,
            expected_error_fragment=(
                "sqltide_local.toml targets.private contains unsupported keys: production"
            ),
        ),
        LocalConfigurationErrorTestCase(
            description="rejects a local destruction limit override",
            local_contents="""
            [targets.private.destruction]
            max_table_size_to_drop = "100GiB"
            """,
            expected_error_fragment=(
                "sqltide_local.toml targets.private contains unsupported keys: destruction"
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_target_scoped_local_mode_when_loading_then_it_rejects_non_project_scope(
    test_case: LocalConfigurationErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "analytics"\ndefault_target = "dev"\n[targets.dev]\n',
    )
    write_local_toml(project_dir=tmp_path, contents=test_case.local_contents)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        LegacyProjectConfigurationTestCase(
            description="rejects the pre-rename project file",
            expected_error_fragment="streambuild_project.toml is not supported",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_legacy_yaml_when_loading_then_requires_toml_conversion(
    test_case: LegacyProjectConfigurationTestCase,
    tmp_path: Path,
) -> None:
    write_legacy_project_file(project_dir=tmp_path)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        MixedProjectConfigurationTestCase(
            description="rejects the pre-rename project file next to the new one",
            expected_error_fragment="Rename it to sqltide_project.toml. Remove the old file.",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_mixed_project_formats_when_loading_then_it_rejects_both(
    test_case: MixedProjectConfigurationTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "analytics"\ndefault_target = "dev"\n[targets.dev]\n',
    )
    write_legacy_project_file(project_dir=tmp_path)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        MissingProjectConfigurationTestCase(
            description="reports the required TOML path when no project exists",
            expected_error_fragment="Project config not found: .*sqltide_project.toml",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_missing_project_when_loading_then_it_reports_required_toml(
    test_case: MissingProjectConfigurationTestCase,
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        UnknownTargetTestCase(
            description="rejects a selected target absent from project and local config",
            selected_target="missing",
            expected_error_fragment="Unknown target 'missing'",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unknown_target_when_resolving_then_it_fails_before_connection(
    test_case: UnknownTargetTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents='name = "analytics"\ndefault_target = "dev"\n[targets.dev]\n',
    )
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        resolve_effective_project_configuration(
            loaded=loaded,
            selected_target=test_case.selected_target,
            cli_variables={},
            environment={},
        )


@pytest.mark.parametrize(
    "test_case",
    [
        InterpolationSuccessTestCase(
            description="preserves a recursively resolved native whole-value scalar",
            values=(("alias", "${count}"), ("count", 7)),
            environment=(),
            input_value="${alias}",
            expected_value=7,
        ),
        InterpolationSuccessTestCase(
            description="renders effective variables and environment values into text",
            values=(("suffix", "events"),),
            environment=(("HOST", "warehouse"),),
            input_value="https://${ENV:HOST}/${suffix}",
            expected_value="https://warehouse/events",
        ),
        InterpolationSuccessTestCase(
            description="preserves a structured variable as a mapping for macro consumption",
            values=(("mapping", {"role": "analyst"}),),
            environment=(),
            input_value="${mapping}",
            expected_value={"role": "analyst"},
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_interpolation_values_when_resolving_then_it_preserves_expected_type(
    test_case: InterpolationSuccessTestCase,
) -> None:
    environment: dict[str, str] = dict(test_case.environment)
    variables: dict[str, object] = resolve_variable_values(
        values=dict(test_case.values),
        environment=environment,
        field_path_prefix="sqltide_project.toml",
    )

    result: object = interpolate_config_value(
        value=test_case.input_value,
        variables=variables,
        environment=environment,
        field_path="sqltide_project.toml test.value",
    )

    assert result == test_case.expected_value


@pytest.mark.parametrize(
    "test_case",
    [
        InterpolationErrorTestCase(
            description="rejects a missing project variable",
            values=(),
            environment=(),
            input_value="${missing}",
            expected_error_fragment="test.value references unknown project variable 'missing'",
        ),
        InterpolationErrorTestCase(
            description="rejects a missing environment variable",
            values=(),
            environment=(),
            input_value="${ENV:MISSING}",
            expected_error_fragment="references missing environment variable 'MISSING'",
        ),
        InterpolationErrorTestCase(
            description="rejects an unsupported namespace",
            values=(),
            environment=(),
            input_value="${CTX:value}",
            expected_error_fragment="uses unsupported interpolation namespace 'CTX'",
        ),
        InterpolationErrorTestCase(
            description="rejects recursive variable cycles",
            values=(("left", "${right}"), ("right", "${left}")),
            environment=(),
            input_value="unused",
            expected_error_fragment="variable interpolation cycle: left -> right -> left",
        ),
        InterpolationErrorTestCase(
            description="rejects an object interpolated into text",
            values=(("mapping", {"key": "value"}),),
            environment=(),
            input_value="prefix-${mapping}",
            expected_error_fragment="cannot interpolate an object or array into text",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_interpolation_when_resolving_then_it_reports_field_context(
    test_case: InterpolationErrorTestCase,
) -> None:
    environment: dict[str, str] = dict(test_case.environment)

    with pytest.raises(ProjectConfigError, match=test_case.expected_error_fragment):
        variables: dict[str, object] = resolve_variable_values(
            values=dict(test_case.values),
            environment=environment,
            field_path_prefix="sqltide_project.toml",
        )
        interpolate_config_value(
            value=test_case.input_value,
            variables=variables,
            environment=environment,
            field_path="sqltide_project.toml test.value",
        )


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectFreshnessDefaultTestCase(
            description="parses a project default freshness policy",
            defaults_toml='[defaults.freshness]\nwarn_after = "15m"\nerror_after = "1h"',
            expected_freshness=SourceFreshnessPolicy(warn_after="15m", error_after="1h"),
        ),
        ProjectFreshnessDefaultTestCase(
            description="parses a warn-only project default freshness policy",
            defaults_toml='[defaults.freshness]\nwarn_after = "12h"',
            expected_freshness=SourceFreshnessPolicy(warn_after="12h"),
        ),
        ProjectFreshnessDefaultTestCase(
            description="defaults an absent freshness policy to none",
            defaults_toml='[defaults]\nmanaged_source_ttl = "_replay_landed_at + INTERVAL 1 DAY"',
            expected_freshness=None,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_defaults_freshness_when_loading_then_returns_expected_policy(
    test_case: ProjectFreshnessDefaultTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=_FRESHNESS_PROJECT_TOML_TEMPLATE.format(defaults_toml=test_case.defaults_toml),
    )

    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    assert loaded.project.defaults.freshness == test_case.expected_freshness


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectFreshnessErrorTestCase(
            description="rejects a malformed freshness duration",
            defaults_toml='[defaults.freshness]\nwarn_after = "soon"',
            expected_error_fragment="must be a duration like",
        ),
        ProjectFreshnessErrorTestCase(
            description="rejects warn_after exceeding error_after",
            defaults_toml='[defaults.freshness]\nwarn_after = "2h"\nerror_after = "1h"',
            expected_error_fragment="warn_after must not exceed error_after",
        ),
        ProjectFreshnessErrorTestCase(
            description="rejects an empty freshness mapping",
            defaults_toml="[defaults.freshness]",
            expected_error_fragment="must set at least one of warn_after or error_after",
        ),
        ProjectFreshnessErrorTestCase(
            description="rejects unknown freshness keys",
            defaults_toml='[defaults.freshness]\ncheck_every = "5m"',
            expected_error_fragment="unknown keys: check_every",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_defaults_freshness_when_loading_then_it_raises_specific_error(
    test_case: ProjectFreshnessErrorTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(
        project_dir=tmp_path,
        contents=_FRESHNESS_PROJECT_TOML_TEMPLATE.format(defaults_toml=test_case.defaults_toml),
    )

    with pytest.raises(PipelineDiscoveryError, match=test_case.expected_error_fragment):
        load_project_configuration(project_dir=tmp_path)


@pytest.mark.parametrize(
    "test_case",
    [
        ProjectConnectionSettingsTestCase(
            description="retains an interpolated target connection settings table",
            project_contents="""
            name = "analytics"
            adapter = "clickhouse"
            default_target = "uat"

            [connection]
            host = "project-host"
            port = 8123
            username = "project-user"
            password = "project-secret"

            [vars]
            replay_threads = "8"

            [targets.uat]
            database = "analytics"

            [targets.uat.connection.settings]
            max_threads = "${replay_threads}"
            max_memory_usage = 16000000000
            """,
            expected_settings=(
                ("max_memory_usage", "16000000000"),
                ("max_threads", "8"),
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_target_connection_settings_when_resolving_then_nested_table_is_retained(
    test_case: ProjectConnectionSettingsTestCase,
    tmp_path: Path,
) -> None:
    write_project_toml(project_dir=tmp_path, contents=test_case.project_contents)
    loaded: LoadedProjectConfiguration = load_project_configuration(project_dir=tmp_path)

    effective: EffectiveProjectConfiguration = resolve_effective_project_configuration(
        loaded=loaded,
        selected_target=None,
        cli_variables={},
        environment={},
    )
    values: Mapping[str, object] = dict(effective.connection.values)
    settings: Mapping[str, object] = cast(Mapping[str, object], values["settings"])
    resolved: tuple[tuple[str, str], ...] = tuple(
        sorted(
            (
                name,
                str(
                    interpolate_config_value(
                        value=setting,
                        variables=dict(effective.variables),
                        environment={},
                        field_path=f"connection.settings.{name}",
                    )
                ),
            )
            for name, setting in settings.items()
        )
    )

    assert resolved == test_case.expected_settings
