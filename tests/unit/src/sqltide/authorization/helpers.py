"""Scenario builders for operational authorization tests."""

from pathlib import Path

from sqltide._native import ControlStore
from sqltide.auth.models import AuthenticatedRequest, Principal, UserAccount
from sqltide.auth.types import AuthenticationSource
from sqltide.authorization.models import AuthorizationRequest
from sqltide.compiler.access.models import (
    CompiledAccessGrant,
    CompiledAccessPolicy,
    CompiledAccessRole,
)
from sqltide.compiler.access.types import GrantScope, Permission
from tests.unit.src.sqltide.auth.helpers import build_control_store
from tests.unit.src.sqltide.authorization._test_types import AuthorizationScenario


def admin_without_policy(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    return store, _request(account=account, roles=("admin",), policy=None)


def viewer_without_policy(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    return store, _request(account=account, roles=("viewer",), policy=None)


def assigned_project_role(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="operator",
        target_name=None,
        actor_user_id=None,
    )
    return (
        store,
        _request(
            account=account,
            roles=("viewer",),
            policy=_policy(_project_role(name="operator")),
        ),
    )


def target_mismatched_assignment(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="operator",
        target_name="test",
        actor_user_id=None,
    )
    return (
        store,
        _request(
            account=account,
            roles=("viewer",),
            policy=_policy(_project_role(name="operator")),
        ),
    )


def stale_assignment(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="removed_role",
        target_name=None,
        actor_user_id=None,
    )
    return (
        store,
        _request(
            account=account,
            roles=("viewer",),
            policy=_policy(_project_role(name="current_role")),
        ),
    )


def collectively_covered_pipelines(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="ingestion_operator",
        target_name=None,
        actor_user_id=None,
    )
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="reporting_operator",
        target_name="prod",
        actor_user_id=None,
    )
    return (
        store,
        _pipeline_request(
            account=account,
            policy=_policy(
                _pipeline_role(name="ingestion_operator", pipeline="ingestion"),
                _pipeline_role(name="reporting_operator", pipeline="reporting"),
            ),
            affected_pipelines=("reporting", "ingestion"),
        ),
    )


def partially_covered_pipelines(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="ingestion_operator",
        target_name=None,
        actor_user_id=None,
    )
    return (
        store,
        _pipeline_request(
            account=account,
            policy=_policy(_pipeline_role(name="ingestion_operator", pipeline="ingestion")),
            affected_pipelines=("reporting", "ingestion"),
        ),
    )


def project_scoped_quality_grant(*, tmp_path: Path) -> AuthorizationScenario:
    store, account = _store_with_account(tmp_path=tmp_path)
    store.grant_project_role(
        user_id=account.user_id,
        project_name="analytics",
        role_name="quality_project",
        target_name=None,
        actor_user_id=None,
    )
    quality_role: CompiledAccessRole = CompiledAccessRole(
        name="quality_project",
        description=None,
        grants=(
            CompiledAccessGrant(
                permissions=(Permission.QUALITY_AUDIT_RUN,), scope=GrantScope.PROJECT
            ),
        ),
    )
    return (
        store,
        AuthorizationRequest(
            authenticated=_authenticated(account=account, roles=("viewer",)),
            permission=Permission.QUALITY_AUDIT_RUN,
            project_name="analytics",
            target_name="prod",
            grant_scope=None,
            affected_pipelines=("reporting", "ingestion"),
            policy=_policy(quality_role),
        ),
    )


def _store_with_account(*, tmp_path: Path) -> tuple[ControlStore, UserAccount]:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    return store, store.create_user(username="alice", roles=("viewer",))


def _request(
    *,
    account: UserAccount,
    roles: tuple[str, ...],
    policy: CompiledAccessPolicy | None,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        authenticated=_authenticated(account=account, roles=roles),
        permission=Permission.PROJECT_RELOAD,
        project_name="analytics",
        target_name="prod",
        grant_scope=GrantScope.PROJECT,
        affected_pipelines=(),
        policy=policy,
    )


def _pipeline_request(
    *,
    account: UserAccount,
    policy: CompiledAccessPolicy,
    affected_pipelines: tuple[str, ...],
) -> AuthorizationRequest:
    return AuthorizationRequest(
        authenticated=_authenticated(account=account, roles=("viewer",)),
        permission=Permission.BUILD_DIRECT_RUN,
        project_name="analytics",
        target_name="prod",
        grant_scope=None,
        affected_pipelines=affected_pipelines,
        policy=policy,
    )


def _authenticated(*, account: UserAccount, roles: tuple[str, ...]) -> AuthenticatedRequest:
    return AuthenticatedRequest(
        principal=Principal(
            user_id=account.user_id,
            username=account.username,
            display_name=account.display_name,
            email=account.email,
            authentication_source=AuthenticationSource.PASSWORD,
        ),
        roles=roles,
    )


def _policy(*roles: CompiledAccessRole) -> CompiledAccessPolicy:
    return CompiledAccessPolicy(roles=roles, fingerprint="test-policy")


def _project_role(*, name: str) -> CompiledAccessRole:
    return CompiledAccessRole(
        name=name,
        description=None,
        grants=(
            CompiledAccessGrant(permissions=(Permission.PROJECT_RELOAD,), scope=GrantScope.PROJECT),
        ),
    )


def _pipeline_role(*, name: str, pipeline: str) -> CompiledAccessRole:
    return CompiledAccessRole(
        name=name,
        description=None,
        grants=(
            CompiledAccessGrant(permissions=(Permission.BUILD_DIRECT_RUN,), pipelines=(pipeline,)),
        ),
    )
