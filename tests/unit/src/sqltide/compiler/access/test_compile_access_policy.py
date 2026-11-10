"""Behavior tests for strict project access-policy compilation."""

import re
from pathlib import Path

import pytest

import sqltide._native as _native
from sqltide._native import build_compiler_adapter_profile
from sqltide.adapters.clickhouse.classes.clickhouse_adapter import ClickHouseAdapter
from sqltide.compiler.access.exceptions import AccessPolicyError
from sqltide.compiler.access.models import CompiledAccessPolicy
from sqltide.compiler.access.types import GrantScope, Permission
from sqltide.compiler.discovery.models import DiscoveredProjectFile
from tests.unit.src.sqltide.compiler.access._test_types import (
    AccessPolicyTestCase,
    EquivalentAccessPoliciesTestCase,
    InvalidAccessPolicyTestCase,
    MissingAccessPolicyTestCase,
)
from tests.unit.src.sqltide.compiler.access.helpers import (
    access_source_file,
    compile_access_policy,
)
from tests.unit.src.sqltide.compiler.pipeline.helpers import write_compilation_project


@pytest.mark.parametrize(
    "test_case",
    [
        MissingAccessPolicyTestCase(
            description="project without access file",
            pipeline_names=frozenset({"ingestion"}),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_access_file_when_compiling_then_policy_is_absent(
    test_case: MissingAccessPolicyTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    write_compilation_project(project_dir)

    policy: CompiledAccessPolicy | None = _native.analyze_project(
        pipelines_root=project_dir / "pipelines",
        loaded_project=_native.load_project_input_for_path(path=project_dir),
        adapter_profile=build_compiler_adapter_profile(ClickHouseAdapter()),
    ).access_policy

    assert policy is test_case.expected_policy


@pytest.mark.parametrize(
    "test_case",
    [
        AccessPolicyTestCase(
            description="pipeline and target grants",
            contents="""roles:
  operator:
    description: Operate ingestion
    grants:
      - pipelines: [reporting, ingestion]
        permissions: [deployment.create, build.direct.run, pipeline.destroy]
      - scope: target
        permissions: [deployment.cleanup, target.reset]
""",
            pipeline_names=frozenset({"ingestion", "reporting"}),
            expected_pipeline_names=("ingestion", "reporting"),
            expected_permissions=("build.direct.run", "deployment.create", "pipeline.destroy"),
            expected_scope="target",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_valid_access_file_when_compiling_then_policy_is_normalized_and_immutable(
    test_case: AccessPolicyTestCase,
    tmp_path: Path,
) -> None:
    policy: CompiledAccessPolicy | None = compile_access_policy(
        source_file=access_source_file(tmp_path=tmp_path, contents=test_case.contents),
        pipeline_names=test_case.pipeline_names,
    )

    assert policy is not None
    assert policy.roles[0].name == "operator"
    assert policy.roles[0].grants[0].pipelines == test_case.expected_pipeline_names
    assert policy.roles[0].grants[0].permissions == tuple(
        Permission(value) for value in test_case.expected_permissions
    )
    assert policy.roles[0].grants[1].scope == GrantScope(test_case.expected_scope)


@pytest.mark.parametrize(
    "test_case",
    [
        EquivalentAccessPoliciesTestCase(
            description="formatting and sequence order differ",
            first_contents="""roles:
  operator:
    grants:
      - pipelines: [reporting, ingestion]
        permissions: [deployment.create, build.direct.run]
""",
            second_contents="""roles:
  operator:
    grants:
      - permissions:
          - build.direct.run
          - deployment.create
        pipelines:
          - ingestion
          - reporting
""",
            pipeline_names=frozenset({"ingestion", "reporting"}),
            expected_fingerprints_equal=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_semantically_equivalent_files_when_compiling_then_fingerprints_match(
    test_case: EquivalentAccessPoliciesTestCase,
    tmp_path: Path,
) -> None:
    first: CompiledAccessPolicy | None = compile_access_policy(
        source_file=access_source_file(tmp_path=tmp_path, contents=test_case.first_contents),
        pipeline_names=test_case.pipeline_names,
    )
    second: CompiledAccessPolicy | None = compile_access_policy(
        source_file=access_source_file(tmp_path=tmp_path, contents=test_case.second_contents),
        pipeline_names=test_case.pipeline_names,
    )

    assert first is not None
    assert second is not None
    assert (first.fingerprint == second.fingerprint) is test_case.expected_fingerprints_equal


@pytest.mark.parametrize(
    "test_case",
    [
        InvalidAccessPolicyTestCase(
            description="unknown top-level key",
            contents="roles: {}\nversion: 1\n",
            expected_message="unknown key(s): version",
        ),
        InvalidAccessPolicyTestCase(
            description="duplicate role mapping key",
            contents="""roles:
  operator: {grants: [{scope: project, permissions: [project.reload]}]}
  operator: {grants: [{scope: project, permissions: [project.reload]}]}
""",
            expected_message="duplicate mapping key 'operator'",
            expected_line=3,
        ),
        InvalidAccessPolicyTestCase(
            description="YAML anchor",
            contents="""roles:
  operator: &operator
    grants: [{scope: project, permissions: [project.reload]}]
""",
            expected_message="YAML anchors are not allowed",
            expected_line=2,
        ),
        InvalidAccessPolicyTestCase(
            description="YAML merge key",
            contents="""roles:
  operator:
    <<: {description: merged}
    grants: [{scope: project, permissions: [project.reload]}]
""",
            expected_message="YAML merge keys are not allowed",
            expected_line=3,
        ),
        InvalidAccessPolicyTestCase(
            description="unknown permission",
            contents="""roles:
  operator:
    grants: [{scope: project, permissions: [project.destroy]}]
""",
            expected_message="unknown permission 'project.destroy'",
        ),
        InvalidAccessPolicyTestCase(
            description="unknown pipeline",
            contents="""roles:
  operator:
    grants: [{pipelines: [missing], permissions: [build.direct.run]}]
""",
            expected_message="references unknown pipelines: missing",
        ),
        InvalidAccessPolicyTestCase(
            description="duplicate permission",
            contents="""roles:
  operator:
    grants:
      - scope: project
        permissions: [project.reload, project.reload]
""",
            expected_message="permissions contains duplicate values",
        ),
        InvalidAccessPolicyTestCase(
            description="ambiguous grant scope",
            contents="""roles:
  operator:
    grants:
      - scope: project
        pipelines: [ingestion]
        permissions: [project.reload]
""",
            expected_message="exactly one of 'pipelines' or 'scope'",
        ),
        InvalidAccessPolicyTestCase(
            description="invalid explicit scope",
            contents="""roles:
  operator:
    grants: [{scope: organization, permissions: [project.reload]}]
""",
            expected_message="received 'organization'",
        ),
        InvalidAccessPolicyTestCase(
            description="system-only permission",
            contents="""roles:
  operator:
    grants: [{scope: project, permissions: [role.assign]}]
""",
            expected_message="cannot grant system-only permission 'role.assign'",
        ),
        InvalidAccessPolicyTestCase(
            description="protected system role",
            contents="""roles:
  admin:
    grants: [{scope: project, permissions: [project.reload]}]
""",
            expected_message="role 'admin' is protected",
        ),
        InvalidAccessPolicyTestCase(
            description="target permission on pipeline grant",
            contents="""roles:
  operator:
    grants: [{pipelines: [ingestion], permissions: [build.kill]}]
""",
            expected_message="cannot grant pipeline permission(s): build.kill",
        ),
        InvalidAccessPolicyTestCase(
            description="pipeline permission at target scope",
            contents="""roles:
  operator:
    grants: [{scope: target, permissions: [build.direct.run]}]
""",
            expected_message="cannot grant build.direct.run at target scope",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_access_file_when_compiling_then_precise_error_is_raised(
    test_case: InvalidAccessPolicyTestCase,
    tmp_path: Path,
) -> None:
    source_file: DiscoveredProjectFile = access_source_file(
        tmp_path=tmp_path, contents=test_case.contents
    )

    with pytest.raises(AccessPolicyError, match=re.escape(test_case.expected_message)) as caught:
        compile_access_policy(
            source_file=source_file,
            pipeline_names=frozenset({"ingestion"}),
        )

    assert caught.value.location.path == source_file.file_path
    assert caught.value.location.line == test_case.expected_line


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
