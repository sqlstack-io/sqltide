from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sqltide._native as _native
import sqltide.dev_server.classes.dev_server_state as dev_server_state_module
from sqltide._native import run_dev_server
from sqltide.auth.models import AuthSettings
from sqltide.auth.types import AuthenticationMode
from tests.unit.src.sqltide.dev_server.main._test_types import AuthBindTestCase

_INSECURE_COOKIE_MESSAGE: str = "insecure password cookies may only be used on a loopback address"


@pytest.mark.parametrize(
    "test_case",
    [
        AuthBindTestCase(
            description="only loopback names and addresses",
            expected_result=(True, True, True, True, True, False, False),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_bind_host_when_classifying_loopback_then_only_loopback_is_detected(
    test_case: AuthBindTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Without a built UI a loopback bind stops at the asset check instead of the cookie check.
    monkeypatch.setattr(_native, "static_assets_root", lambda: tmp_path)
    monkeypatch.setattr(_native, "static_assets_present", lambda *, assets_root: False)

    def is_loopback(host: str) -> bool:
        run_dev_server(
            run_compile=MagicMock(),
            connection=None,
            observation_connection=None,
            database=None,
            project_dir=tmp_path,
            host=host,
            port=8000,
            reporter=MagicMock(),
            auth_settings=AuthSettings(
                mode=AuthenticationMode.PASSWORD,
                control_store_url="unused",
                session_cookie_secure=False,
            ),
        )
        return _INSECURE_COOKIE_MESSAGE not in capsys.readouterr().err

    assert (
        is_loopback("127.0.0.1"),
        is_loopback("::1"),
        is_loopback("localhost"),
        is_loopback("::ffff:127.0.0.1"),
        is_loopback("::1%lo"),
        is_loopback("0.0.0.0"),
        is_loopback("sqltide.internal"),
    ) == test_case.expected_result


@pytest.mark.parametrize(
    "test_case",
    [
        AuthBindTestCase(
            description="disabled shared bind starts and warns",
            expected_result=(0, True),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_disabled_auth_when_starting_on_shared_bind_then_startup_proceeds_with_warning(
    test_case: AuthBindTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application: MagicMock = MagicMock()
    monkeypatch.setattr(_native, "static_assets_root", lambda: tmp_path)
    monkeypatch.setattr(_native, "static_assets_present", lambda *, assets_root: True)
    monkeypatch.setattr(dev_server_state_module, "DevServerState", MagicMock())
    monkeypatch.setattr(_native, "create_dev_app", MagicMock(return_value=application))

    result: int = run_dev_server(
        run_compile=MagicMock(),
        connection=None,
        observation_connection=None,
        database=None,
        project_dir=tmp_path,
        host="0.0.0.0",
        port=8000,
        reporter=MagicMock(),
        auth_settings=AuthSettings(
            mode=AuthenticationMode.DISABLED,
            control_store_url="unused",
        ),
    )

    warned: bool = "authentication disabled; all clients have administrator access" in (
        capsys.readouterr().err
    )
    assert (result, warned) == test_case.expected_result
    application.serve.assert_called_once_with("0.0.0.0", 8000)


@pytest.mark.parametrize(
    "test_case",
    [AuthBindTestCase(description="insecure shared cookie rejected", expected_result=1)],
    ids=lambda case: case.description,
)
def test_given_insecure_password_cookie_when_starting_on_shared_bind_then_startup_is_rejected(
    test_case: AuthBindTestCase, tmp_path: Path
) -> None:
    result: int = run_dev_server(
        run_compile=MagicMock(),
        connection=None,
        observation_connection=None,
        database=None,
        project_dir=tmp_path,
        host="0.0.0.0",
        port=8000,
        reporter=MagicMock(),
        auth_settings=AuthSettings(
            mode=AuthenticationMode.PASSWORD,
            control_store_url="unused",
            session_cookie_secure=False,
        ),
    )

    assert result == test_case.expected_result


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
