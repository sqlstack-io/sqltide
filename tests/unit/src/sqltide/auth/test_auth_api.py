import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.exceptions import AuthConfigurationError
from sqltide.auth.models import AuthSettings, UserAccount
from sqltide.auth.types import AuthenticationMode, UnknownUserPolicy
from tests.unit.src.sqltide.auth._test_types import AuthTestCase
from tests.unit.src.sqltide.auth.helpers import build_auth_client, build_control_store
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="local administrator", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_local_mode_when_reading_me_then_deterministic_admin_is_returned(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url="unused",
        ),
        store=store,
    )

    response: NativeResponse = client.get("/api/auth/me")

    assert response.status_code == test_case.expected_result
    assert response.json()["user"]["username"] == "local"
    assert response.json()["roles"] == ["admin"]
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="new proxy viewer", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_proxy_mode_when_identity_is_new_then_viewer_is_provisioned(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    settings: AuthSettings = AuthSettings(
        mode=AuthenticationMode.TRUSTED_PROXY,
        control_store_url="unused",
        username_header="X-SQLTide-User",
    )
    client: NativeTestClient = build_auth_client(settings=settings, store=store)

    missing: NativeResponse = client.get("/api/auth/me")
    first: NativeResponse = client.get("/api/auth/me", headers={"X-SQLTide-User": "Alice"})
    second: NativeResponse = client.get("/api/auth/me", headers={"X-SQLTide-User": "alice"})

    assert missing.status_code == 401
    assert first.status_code == test_case.expected_result
    assert first.json()["roles"] == ["viewer"]
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert len(store.list_users()) == 1
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="unknown proxy denied", expected_result=403)],
    ids=lambda case: case.description,
)
def test_given_proxy_deny_policy_when_identity_is_new_then_request_is_forbidden(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.TRUSTED_PROXY,
            control_store_url="unused",
            unknown_user_policy=UnknownUserPolicy.DENY,
        ),
        store=store,
    )

    response: NativeResponse = client.get("/api/auth/me", headers={"X-SQLTide-User": "alice"})

    assert response.status_code == test_case.expected_result
    assert store.list_users() == ()
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="valid proxy mutation proof", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_proxy_principal_when_posting_then_request_proof_is_required(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.TRUSTED_PROXY,
            control_store_url="unused",
        ),
        store=store,
    )
    headers: dict[str, str] = {"X-SQLTide-User": "alice"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    rejected: NativeResponse = client.post("/api/auth/logout", headers=headers)
    accepted: NativeResponse = client.post(
        "/api/auth/logout",
        headers={**headers, "X-SQLTide-CSRF": "trusted-proxy"},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="password session created", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_password_mode_when_logging_in_then_cookie_and_csrf_protect_session(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
        store=store,
    )

    unauthenticated: NativeResponse = client.get("/api/auth/me")
    login: NativeResponse = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct sample battery staple"},
    )
    me: NativeResponse = client.get("/api/auth/me")
    rejected_logout: NativeResponse = client.post("/api/auth/logout")
    logout: NativeResponse = client.post(
        "/api/auth/logout",
        headers={"X-SQLTide-CSRF": login.json()["csrfToken"]},
    )

    assert unauthenticated.status_code == 401
    assert login.status_code == test_case.expected_result
    assert "sqltide_session=" in login.headers["set-cookie"]
    assert me.json()["user"]["authenticationSource"] == "password"
    assert rejected_logout.status_code == 403
    assert logout.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="ordinary request fan-out reuses one resolved session", expected_result=1
        )
    ],
    ids=lambda case: case.description,
)
def test_given_active_password_session_when_requests_fan_out_then_resolution_is_cached(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    account: UserAccount = store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
        store=store,
    )
    client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct sample battery staple"},
    )

    with patch.object(store, "resolve_session", wraps=store.resolve_session) as resolved:
        resolver: Mock = resolved
        first: NativeResponse = client.get("/api/auth/me")
        second: NativeResponse = client.get("/api/auth/me")

        assert first.status_code == 200
        assert second.status_code == 200
        assert resolver.call_count == test_case.expected_result

        store.grant_role(user_id=account.user_id, role_name="admin", actor_user_id=None)
        changed: NativeResponse = client.get("/api/auth/me")

        assert changed.json()["roles"] == ["admin", "viewer"]
        assert resolver.call_count == 2

        token: str = client.cookies["sqltide_session"]
        store.revoke_session(token=token)
        assert client.get("/api/auth/me").status_code == 401
        assert resolver.call_count == 3
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="recent session activity does not rewrite last seen", expected_result=200
        )
    ],
    ids=lambda case: case.description,
)
def test_given_recent_password_session_when_reading_pages_then_last_seen_is_not_rewritten(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
        store=store,
    )
    login: NativeResponse = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct sample battery staple"},
    )
    database_path: Path = tmp_path / "control.db"
    with sqlite3.connect(database_path) as connection:
        before: str = str(
            connection.execute("SELECT last_seen_at FROM sqltide_sessions").fetchone()[0]
        )

    first: NativeResponse = client.get("/api/auth/me")
    second: NativeResponse = client.get("/api/auth/me")
    with sqlite3.connect(database_path) as connection:
        after: str = str(
            connection.execute("SELECT last_seen_at FROM sqltide_sessions").fetchone()[0]
        )

    assert login.status_code == test_case.expected_result
    assert first.status_code == test_case.expected_result
    assert second.status_code == test_case.expected_result
    assert after == before
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="secure session cookie", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_password_mode_when_logging_in_then_session_cookie_is_secure_by_default(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
        ),
        store=store,
    )

    response: NativeResponse = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct sample battery staple"},
    )

    assert response.status_code == test_case.expected_result
    assert "Secure" in response.headers["set-cookie"]
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="canonical username throttling",
            expected_result=(401, 401, 401, 401, 401, 429),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_username_variants_when_login_keeps_failing_then_shared_limit_applies(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
        store=store,
    )

    attempts: list[NativeResponse] = [
        client.post(
            "/api/auth/login",
            json={"username": username, "password": "incorrect password"},
        )
        for username in ("alice", " alice", "alice ", "ALICE", " Alice ", "  alice")
    ]

    assert tuple(response.status_code for response in attempts) == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(
            description="successful account login preserves IP failures", expected_result=429
        )
    ],
    ids=lambda case: case.description,
)
def test_given_ip_failures_when_one_login_succeeds_then_ip_limit_still_applies(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    store.create_user(
        username="alice",
        password="correct sample battery staple",
        roles=("viewer",),
    )
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
        store=store,
    )
    for index in range(19):
        response: NativeResponse = client.post(
            "/api/auth/login",
            json={"username": f"unknown-{index}", "password": "incorrect password"},
        )
        assert response.status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct sample battery staple"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "another-unknown", "password": "incorrect password"},
        ).status_code
        == 401
    )

    blocked: NativeResponse = client.post(
        "/api/auth/login",
        json={"username": "last-unknown", "password": "incorrect password"},
    )

    assert blocked.status_code == test_case.expected_result
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [
        AuthTestCase(description="admin auto-provisioning role", expected_result="admin"),
        AuthTestCase(description="unknown auto-provisioning role", expected_result="operator"),
    ],
    ids=lambda case: case.description,
)
def test_given_unsafe_proxy_default_role_when_configuring_then_configuration_is_rejected(
    test_case: AuthTestCase,
) -> None:
    with pytest.raises(AuthConfigurationError, match="default role must be 'viewer'"):
        AuthSettings(
            mode=AuthenticationMode.TRUSTED_PROXY,
            control_store_url="unused",
            default_role=str(test_case.expected_result),
        )


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="account created and audited", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_local_admin_when_managing_account_then_api_persists_and_audits(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url="unused",
        ),
        store=store,
    )

    created: NativeResponse = client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "authenticationSource": "password",
            "password": "correct sample battery staple",
            "roles": ["viewer"],
        },
    )
    users: NativeResponse = client.get("/api/admin/users")
    audit: NativeResponse = client.get("/api/admin/audit")

    assert created.status_code == test_case.expected_result
    assert users.json()[0]["username"] == "alice"
    assert users.json()[0]["authenticationSources"] == ["password"]
    assert any(record["operation"] == "user.created" for record in audit.json())
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="short initial password", expected_result=400)],
    ids=lambda case: case.description,
)
def test_given_short_initial_password_when_creating_account_then_validation_is_returned(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url="unused",
        ),
        store=store,
    )

    response: NativeResponse = client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "authenticationSource": "password",
            "password": "short",
            "roles": ["viewer"],
        },
    )

    assert response.status_code == test_case.expected_result
    assert response.json()["detail"] == "Password must contain at least 12 characters"
    assert store.list_users() == ()
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AuthTestCase(description="project role granted, listed, and revoked", expected_result=200)],
    ids=lambda case: case.description,
)
def test_given_local_admin_when_managing_project_roles_then_assignments_round_trip(
    test_case: AuthTestCase, tmp_path: Path
) -> None:
    store: ControlStore = build_control_store(tmp_path=tmp_path)
    account: UserAccount = store.create_user(username="alice", roles=("viewer",))
    client: NativeTestClient = build_auth_client(
        settings=AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url="unused",
        ),
        store=store,
    )

    granted: NativeResponse = client.post(
        f"/api/admin/users/{account.user_id}/project-roles",
        json={"projectName": "analytics", "role": "operator", "targetName": "prod"},
    )
    duplicate: NativeResponse = client.post(
        f"/api/admin/users/{account.user_id}/project-roles",
        json={"projectName": "analytics", "role": "operator", "targetName": "prod"},
    )
    listed: NativeResponse = client.get(
        f"/api/admin/users/{account.user_id}/project-roles",
        params={"project": "analytics"},
    )
    revoked: NativeResponse = client.delete(
        f"/api/admin/project-roles/{granted.json()['assignmentId']}"
    )
    after_revoke: NativeResponse = client.get(
        f"/api/admin/users/{account.user_id}/project-roles",
        params={"project": "analytics"},
    )

    assert granted.status_code == test_case.expected_result
    assert granted.json()["role"] == "operator"
    assert granted.json()["targetName"] == "prod"
    assert duplicate.status_code == 409
    assert [item["assignmentId"] for item in listed.json()] == [granted.json()["assignmentId"]]
    assert revoked.status_code == 200
    assert revoked.json()["revokedAt"] is not None
    assert after_revoke.json() == []
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
