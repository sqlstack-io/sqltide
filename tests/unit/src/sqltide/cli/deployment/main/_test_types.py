from dataclasses import dataclass


@dataclass(frozen=True)
class DeploymentReadCommandTestCase:
    description: str
    expected_exit_code: int
    expected_output_fragment: str


@dataclass(frozen=True)
class DeploymentPromoteLatestTestCase:
    description: str
    deployments: tuple[tuple[str, str], ...]
    published_deployment_ids: tuple[str, ...]
    expected_exit_code: int
    expected_error_fragments: tuple[str, ...]
    expected_promoted_deployment_ids: tuple[str, ...]
