from dataclasses import dataclass

from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import CatalogRelation


@dataclass(frozen=True)
class CliDevRefactorTestCase:
    description: str
    expected_value: object


@dataclass(frozen=True)
class CliUserErrorPresentationTestCase:
    description: str
    expected_error_fragments: tuple[str, ...]
    expected_exit_code: int


@dataclass(frozen=True)
class CliMainTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliMainJsonTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    handler_name: str | None = None
    handler_output: str = ""


@dataclass(frozen=True)
class CliMainErrorTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_error_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliMainIntegrationTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    expects_json_output: bool = False


@dataclass(frozen=True)
class CliMainEnvResolutionTestCase:
    description: str
    argv: tuple[str, ...]
    env_vars: dict[str, str]
    expected_exit_code: int
    expected_kwargs: dict[str, object]


@dataclass(frozen=True)
class CliMainJsonFlagTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_json_output: bool


@dataclass(frozen=True)
class CliBuildObservationConnectionTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int


@dataclass(frozen=True)
class CliBuildObservationFailureTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    connection_error: str
    expected_error_fragment: str


@dataclass(frozen=True)
class CliSelectorForwardingTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_selectors: tuple[str, ...]
    expected_full_refresh: bool
    expected_deployment_id: str | None


@dataclass(frozen=True)
class CliRenderingTestCase:
    description: str
    expected_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliPublishAtomicityRenderingTestCase:
    description: str
    per_relation_atomic_replace: bool
    graph_atomic_publish: bool
    expected_atomicity: dict[str, bool]


@dataclass(frozen=True)
class CliPlanRenderingBaselineTestCase:
    description: str
    expected_payload: dict[str, object]
    expected_compact_text: str
    expected_verbose_text: str


@dataclass(frozen=True)
class CliHelpBaselineTestCase:
    description: str
    argv: tuple[str, ...]
    expected_sha256: str


@dataclass(frozen=True)
class CliAdapterRejectionTestCase:
    description: str
    project_file_contents: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_error_fragments: tuple[str, ...]
    expected_absent_error_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliAdapterPlanExecutionTestCase:
    description: str
    project_file_contents: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    expected_exit_code: int
    expected_connection: tuple[str, int, str, str]
    expected_catalog_load_count: int
    expected_query_count: int
    expected_connection_closed: bool
    expected_stdout: str
    expected_redacted_secret: str


@dataclass(frozen=True)
class CliPlanPreservationMatrixTestCase:
    description: str
    replay_lineage_mode: str
    pipeline_file_contents: str
    model_file_contents: str
    catalog_relations: tuple[CatalogRelation, ...]
    expected_exit_code: int
    expected_subtree_summary: str
    expected_catalog_load_count: int
    expected_query_count: int


@dataclass(frozen=True)
class CliLazyConnectionTestCase:
    description: str
    project_vars_contents: str
    secret_template: str
    expected_compile_exit_code: int
    expected_plan_exit_code: int
    expected_connect_count: int
    expected_error_fragment: str
    expected_absent_fragment: str


@dataclass(frozen=True)
class CliModeGateErrorTestCase:
    description: str
    argv: tuple[str, ...]
    project_file_contents: str
    expected_error_fragment: str


@dataclass(frozen=True)
class CliModeOverrideTestCase:
    description: str
    argv: tuple[str, ...]
    project_file_contents: str
    local_file_contents: str
    expected_handler_name: str
    expected_exit_code: int
    expected_handler_call_count: int


@dataclass(frozen=True)
class CliProjectSecretRedactionTestCase:
    description: str
    secret: str
    expected_compile_exit_code: int
    expected_plan_exit_code: int
    expected_error_fragment: str


@dataclass(frozen=True)
class CliNestedAuditOptionsTestCase:
    description: str
    argv: tuple[str, ...]
    expected_project_dir: str
    expected_host: str
    expected_port: int
    expected_username: str
    expected_password: str
    expected_database: str
    expected_json: bool
    expected_target: str
    expected_vars: dict[str, object]


@dataclass(frozen=True)
class CliRequiredDeploymentIdTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int


@dataclass(frozen=True)
class CliTargetSelectionTestCase:
    description: str
    argv_suffix: tuple[str, ...]
    local_contents: str
    expected_database_fragment: str


@dataclass(frozen=True)
class CliExpectedErrorRenderingTestCase:
    description: str
    error: AdapterWarehouseError
    expected_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliCommandErrorTestCase:
    description: str
    error_message: str
    expected_exit_code: int
    expected_error_fragments: tuple[str, ...]


@dataclass(frozen=True)
class CliJanitorApplyFlagTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_apply: bool


@dataclass(frozen=True)
class CliReconcileForwardingTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_selectors: tuple[str, ...]
    expected_apply: bool
    expected_json_output: bool


@dataclass(frozen=True)
class CliProjectDefaultsTestCase:
    description: str
    command_name: str
    expected_database: str


@dataclass(frozen=True)
class CliProjectConnectionResolutionTestCase:
    description: str
    expected_project_connection: tuple[str, int, str, str]


@dataclass(frozen=True)
class CliCompileArtifactsTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_output_fragments: tuple[str, ...]
    expected_written_files: tuple[str, ...]
    expected_target_dir_name: str = "target"


@dataclass(frozen=True)
class CliAuditBackfillProjectContextTestCase:
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int
    expected_project_dir_name: str
    expected_pipelines_root_name: str
