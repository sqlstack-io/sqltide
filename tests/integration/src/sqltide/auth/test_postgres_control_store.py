import pytest
from testcontainers.postgres import PostgresContainer

from sqltide._native import ControlStore
from sqltide.auth.models import ProjectRoleAssignment, SessionCredentials, UserAccount
from sqltide.auth.types import AuthenticationSource
from tests.integration.src.sqltide.auth._test_types import PostgresControlStoreTestCase
from tests.integration.src.sqltide.auth.helpers import (
    concurrently_provision_proxy_identity,
    concurrently_revoke_admin_roles,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PostgresControlStoreTestCase(
            description="PostgreSQL account lifecycle", expected_username="alice"
        )
    ],
    ids=lambda case: case.description,
)
def test_given_postgres_store_when_using_accounts_then_matches_sqlite_contract(
    test_case: PostgresControlStoreTestCase,
) -> None:
    with PostgresContainer("postgres:16-alpine") as postgres:
        store: ControlStore = ControlStore(url=postgres.get_connection_url(driver="psycopg"))
        account: UserAccount = store.create_user(
            username="alice",
            password="correct sample battery staple",
            roles=("viewer",),
        )

        login: tuple[UserAccount, SessionCredentials] | None = store.authenticate_password(
            username="alice",
            password="correct sample battery staple",
            session_ttl_seconds=3600,
        )

        assert login is not None
        reloaded, credentials = login
        assert reloaded.username == test_case.expected_username
        assert reloaded.user_id == account.user_id
        assert reloaded.authentication_sources == (AuthenticationSource.PASSWORD,)
        assert store.resolve_session(token=credentials.token) is not None

        store.grant_role(user_id=account.user_id, role_name="admin", actor_user_id=None)
        store.create_user(username="recovery-admin", roles=("admin",))
        store.set_user_active(
            user_id=account.user_id,
            is_active=False,
            actor_user_id=None,
        )

        assert store.resolve_session(token=credentials.token) is None
        disabled: UserAccount | None = store.get_user_by_id(user_id=account.user_id)
        assert disabled is not None
        assert disabled.is_active is False

        assignment: ProjectRoleAssignment = store.grant_project_role(
            user_id=account.user_id,
            project_name="analytics",
            role_name="operator",
            target_name="prod",
            actor_user_id=None,
        )
        assert store.list_project_role_assignments(
            user_id=account.user_id, project_name="analytics"
        ) == (assignment,)

        revoked: ProjectRoleAssignment = store.revoke_project_role(
            assignment_id=assignment.assignment_id, actor_user_id=None
        )
        assert (
            store.list_project_role_assignments(user_id=account.user_id, project_name="analytics")
            == ()
        )
        assert store.list_project_role_assignments(
            user_id=account.user_id,
            project_name="analytics",
            include_revoked=True,
        ) == (revoked,)
        store.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PostgresControlStoreTestCase(
            description="concurrent administrator removal preserves one administrator",
            expected_username="admin-a",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_two_postgres_admins_when_revoked_concurrently_then_last_admin_is_protected(
    test_case: PostgresControlStoreTestCase,
) -> None:
    with PostgresContainer("postgres:16-alpine") as postgres:
        store: ControlStore = ControlStore(url=postgres.get_connection_url(driver="psycopg"))
        first: UserAccount = store.create_user(
            username=test_case.expected_username, roles=("admin",)
        )
        second: UserAccount = store.create_user(username="admin-b", roles=("admin",))

        outcomes: tuple[str, str] = concurrently_revoke_admin_roles(
            store=store, user_ids=(first.user_id, second.user_id)
        )

        assert sorted(outcomes) == ["protected", "revoked"]
        admin_role_counts: tuple[int, ...] = tuple(
            account.roles.count("admin") for account in store.list_users()
        )
        assert sum(admin_role_counts) == 1
        store.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "test_case",
    [
        PostgresControlStoreTestCase(
            description="concurrent proxy provisioning resolves one identity",
            expected_username="alice",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unknown_proxy_identity_when_provisioned_concurrently_then_one_account_is_returned(
    test_case: PostgresControlStoreTestCase,
) -> None:
    with PostgresContainer("postgres:16-alpine") as postgres:
        store: ControlStore = ControlStore(url=postgres.get_connection_url(driver="psycopg"))

        first, second = concurrently_provision_proxy_identity(
            store=store, username=test_case.expected_username
        )

        assert first.user_id == second.user_id
        assert first.username == test_case.expected_username
        assert store.list_users() == (first,)
        store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
