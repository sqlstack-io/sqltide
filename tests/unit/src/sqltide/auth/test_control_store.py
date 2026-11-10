import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from sqltide._native import ControlStore, default_control_store_url
from sqltide.auth.exceptions import AccountConflictError
from sqltide.auth.models import (
    AccountAuditRecord,
    ProjectRoleAssignment,
    ResolvedSession,
    SessionCredentials,
    UserAccount,
)
from sqltide.auth.types import AuthenticationSource
from tests.unit.src.sqltide.auth._test_types import AuthTestCase
from tests.unit.src.sqltide.auth.helpers import build_control_store


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="idempotent role bootstrap", expected_result="alice")],
    ids=lambda case: case.description,
)
def test_given_fresh_store_when_bootstrapping_then_system_roles_are_idempotent(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)

    store.bootstrap()
    account: UserAccount = store.create_user(username="Alice", roles=("viewer",))

    assert account.username == test_case.expected_result
    assert account.roles == ("viewer",)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="proxy identity reused", expected_result=1)],
    ids=lambda case: case.description,
)
def test_given_proxy_identity_when_provisioning_twice_then_one_account_is_reused(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)

    first: UserAccount = store.provision_proxy_user(
        subject="alice",
        username="alice",
        display_name="Alice",
        email="alice@example.com",
        default_role="viewer",
    )
    second: UserAccount = store.provision_proxy_user(
        subject="alice",
        username="alice",
        display_name=None,
        email=None,
        default_role="viewer",
    )

    assert first.user_id == second.user_id
    assert len(store.list_users()) == test_case.expected_result
    assert first.authentication_sources == (AuthenticationSource.TRUSTED_PROXY,)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="password session lifecycle",
            expected_result=AuthenticationSource.PASSWORD,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_password_account_when_authenticating_then_session_resolves_and_revokes(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    account: UserAccount = store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )

    result: tuple[UserAccount, SessionCredentials] | None = store.authenticate_password(
        username="alice",
        password="correct sample battery staple",
        session_ttl_seconds=3600,
    )

    assert result is not None
    authenticated, credentials = result
    assert authenticated.user_id == account.user_id
    resolved: ResolvedSession | None = store.resolve_session(token=credentials.token)
    assert resolved is not None
    assert resolved.principal.authentication_source == test_case.expected_result

    store.revoke_session(token=credentials.token, actor_user_id=account.user_id)

    assert store.resolve_session(token=credentials.token) is None
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="last administrator remains active", expected_result=True)],
    ids=lambda case: case.description,
)
def test_given_last_admin_when_disabling_then_store_rejects_lockout(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    admin: UserAccount = store.create_user(username="admin-user", roles=("admin",))

    with pytest.raises(AccountConflictError, match="last active administrator"):
        store.set_user_active(
            user_id=admin.user_id,
            is_active=False,
            actor_user_id=admin.user_id,
        )

    reloaded: UserAccount | None = store.get_user_by_id(user_id=admin.user_id)
    assert reloaded is not None
    assert reloaded.is_active is test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="unlinked username still conflicts", expected_result=1)],
    ids=lambda case: case.description,
)
def test_given_unlinked_username_when_provisioning_proxy_then_conflict_is_reported(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(username="alice", roles=("viewer",))

    with pytest.raises(AccountConflictError, match="unlinked username"):
        store.provision_proxy_user(
            subject="alice",
            username="alice",
            display_name=None,
            email=None,
            default_role="viewer",
        )

    assert len(store.list_users()) == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="orphan role rejected", expected_result=())],
    ids=lambda case: case.description,
)
def test_given_missing_user_when_granting_role_then_foreign_key_prevents_orphan(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)

    with pytest.raises(AccountConflictError):
        store.grant_role(
            user_id=uuid4(),
            role_name="viewer",
            actor_user_id=None,
        )

    assert store.list_users() == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="all-target and explicit-target project roles",
            expected_result=(("operator", None), ("operator", "prod")),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_project_roles_when_granting_distinct_scopes_then_assignments_are_audited(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    admin: UserAccount = store.create_user(username="admin-user", roles=("admin",))
    alice: UserAccount = store.create_user(username="alice", roles=("viewer",))

    store.grant_project_role(
        user_id=alice.user_id,
        project_name="analytics",
        role_name="operator",
        target_name=None,
        actor_user_id=admin.user_id,
    )
    store.grant_project_role(
        user_id=alice.user_id,
        project_name="analytics",
        role_name="operator",
        target_name="prod",
        actor_user_id=admin.user_id,
    )

    assignments: tuple[ProjectRoleAssignment, ...] = store.list_project_role_assignments(
        user_id=alice.user_id, project_name="analytics"
    )
    project_audits: tuple[AccountAuditRecord, ...] = store.list_audit_records()[-2:]
    assert tuple((item.role_name, item.target_name) for item in assignments) == (
        test_case.expected_result
    )
    assert len(project_audits) == len(assignments)
    assert all(record.actor_user_id == admin.user_id for record in project_audits)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="duplicate active project role", expected_result=1)],
    ids=lambda case: case.description,
)
def test_given_active_project_role_when_granting_duplicate_then_mutation_is_atomic(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    alice: UserAccount = store.create_user(username="alice", roles=("viewer",))
    assignment: ProjectRoleAssignment = store.grant_project_role(
        user_id=alice.user_id,
        project_name="analytics",
        role_name="operator",
        target_name=None,
        actor_user_id=None,
    )

    with pytest.raises(AccountConflictError, match="already assigned"):
        store.grant_project_role(
            user_id=alice.user_id,
            project_name="analytics",
            role_name="operator",
            target_name=None,
            actor_user_id=None,
        )

    assignments: tuple[ProjectRoleAssignment, ...] = store.list_project_role_assignments(
        user_id=alice.user_id, project_name="analytics", include_revoked=True
    )
    audits: tuple[AccountAuditRecord, ...] = store.list_audit_records()[-1:]
    assert assignments == (assignment,)
    assert len(audits) == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="revoked project role history", expected_result=(0, 1))],
    ids=lambda case: case.description,
)
def test_given_project_role_when_revoking_then_membership_is_inactive_and_history_remains(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    alice: UserAccount = store.create_user(username="alice", roles=("viewer",))
    assignment: ProjectRoleAssignment = store.grant_project_role(
        user_id=alice.user_id,
        project_name="analytics",
        role_name="operator",
        target_name="prod",
        actor_user_id=None,
    )

    revoked: ProjectRoleAssignment = store.revoke_project_role(
        assignment_id=assignment.assignment_id, actor_user_id=alice.user_id
    )

    active: tuple[ProjectRoleAssignment, ...] = store.list_project_role_assignments(
        user_id=alice.user_id, project_name="analytics"
    )
    history: tuple[ProjectRoleAssignment, ...] = store.list_project_role_assignments(
        user_id=alice.user_id, project_name="analytics", include_revoked=True
    )
    assert (len(active), len(history)) == test_case.expected_result
    assert revoked.revoked_by == alice.user_id
    assert revoked.revoked_at is not None
    assert history == (revoked,)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="atomic administrator update", expected_result="Original name")],
    ids=lambda case: case.description,
)
def test_given_last_admin_profile_and_disable_when_updating_then_entire_edit_rolls_back(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    admin: UserAccount = store.create_user(
        username="admin-user",
        display_name="Original name",
        roles=("admin",),
    )

    with pytest.raises(AccountConflictError, match="last active administrator"):
        store.update_account(
            user_id=admin.user_id,
            display_name="Changed name",
            email=None,
            is_active=False,
            actor_user_id=admin.user_id,
        )

    reloaded: UserAccount | None = store.get_user_by_id(user_id=admin.user_id)
    assert reloaded is not None
    assert reloaded.display_name == test_case.expected_result
    assert reloaded.is_active is True
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="StreamBuild accounts survive the rename", expected_result="alice")],
    ids=lambda case: case.description,
)
def test_given_streambuild_control_store_when_opening_then_accounts_are_kept(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    streambuild_database: Path = tmp_path / ".streambuild" / "control.db"
    ControlStore(url=f"sqlite:///{streambuild_database}").create_user(
        username="alice", password="correct horse battery", roles=("viewer",)
    )
    with sqlite3.connect(streambuild_database) as connection:
        table_names: list[str] = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for name in table_names:
            renamed: str = "streambuild_" + name.removeprefix("sqltide_")
            connection.execute(f'ALTER TABLE "{name}" RENAME TO "{renamed}"')

    store: ControlStore = ControlStore(url=default_control_store_url(project_dir=tmp_path))
    authenticated: tuple[UserAccount, SessionCredentials] | None = store.authenticate_password(
        username="alice", password="correct horse battery", session_ttl_seconds=60
    )
    promoted: UserAccount = store.grant_role(
        user_id=store.list_users()[0].user_id, role_name="admin", actor_user_id=None
    )

    assert authenticated is not None
    assert authenticated[0].username == test_case.expected_result
    assert promoted.roles == ("admin", "viewer")
    assert not (tmp_path / ".streambuild").exists()
    store.close()
