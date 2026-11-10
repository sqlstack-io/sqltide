from pathlib import Path

import pytest

from sqltide._native import run_cli
from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.models import UserAccount
from tests.unit.src.sqltide.cli.admin._test_types import AdminCliTestCase


@pytest.mark.parametrize(
    "test_case",
    [AdminCliTestCase(description="proxy administrator bootstrap", expected_username="kevinl")],
    ids=lambda case: case.description,
)
def test_given_empty_control_store_when_creating_proxy_admin_then_cli_bootstraps_account(
    test_case: AdminCliTestCase, tmp_path: Path
) -> None:
    url: str = f"sqlite:///{tmp_path / 'control.db'}"

    exit_code: int = run_cli(
        [
            "sqt",
            "admin",
            "--control-store-url",
            url,
            "create-user",
            "--username",
            "KevinL",
            "--authentication-source",
            "trusted_proxy",
            "--role",
            "admin",
        ]
    )

    store: ControlStore = ControlStore(url=url)
    account: UserAccount | None = store.get_user_by_username(username="kevinl")
    assert exit_code == 0
    assert account is not None
    assert account.username == test_case.expected_username
    assert account.roles == ("admin",)
    store.close()


@pytest.mark.parametrize(
    "test_case",
    [AdminCliTestCase(description="administrator grant", expected_username="alice")],
    ids=lambda case: case.description,
)
def test_given_viewer_when_granting_admin_then_cli_updates_existing_account(
    test_case: AdminCliTestCase, tmp_path: Path
) -> None:
    url: str = f"sqlite:///{tmp_path / 'control.db'}"
    store: ControlStore = ControlStore(url=url)
    store.create_user(username="alice", roles=("viewer",))
    store.close()

    exit_code: int = run_cli(
        [
            "sqt",
            "admin",
            "--control-store-url",
            url,
            "grant-role",
            "--username",
            "alice",
            "--role",
            "admin",
        ]
    )

    store = ControlStore(url=url)
    account: UserAccount | None = store.get_user_by_username(username="alice")
    assert exit_code == 0
    assert account is not None
    assert account.username == test_case.expected_username
    assert account.roles == ("admin", "viewer")
    store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
