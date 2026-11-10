from pathlib import Path
from unittest.mock import MagicMock

from sqltide._native import create_dev_app
from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.models import AuthSettings
from sqltide.dev_server.classes.dev_server_state import DevServerState
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeTestClient


def build_control_store(*, tmp_path: Path) -> ControlStore:
    return ControlStore(url=f"sqlite:///{tmp_path / 'control.db'}")


def build_auth_client(*, settings: AuthSettings, store: ControlStore) -> NativeTestClient:
    return NativeTestClient(
        create_dev_app(
            state=DevServerState(run_compile=MagicMock()),
            auth_settings=settings,
            control_store=store,
        )
    )
