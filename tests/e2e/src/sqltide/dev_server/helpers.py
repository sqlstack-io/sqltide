import itertools
import json
import os
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from shutil import copytree
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from clickhouse_connect.driver.client import Client
from playwright.sync_api import Page

from sqltide._native import DevApplication, create_dev_app
from sqltide.auth.classes.control_store import ControlStore
from sqltide.auth.models import UserAccount
from sqltide.auth.types import AuthenticationSource
from sqltide.compiler.pipeline.models import CompileAnalysis
from sqltide.dev_server.classes.dev_server_state import DevServerState
from tests.unit.src.sqltide.dev_server.helpers import (
    build_compile_callable,
    write_dev_server_project,
)

LINEAGE_BROWSER_PROJECT_DIR: Path = Path("tests/fixtures/lineage_browser_project")


def read_json_url(
    url: str,
    *,
    timeout_seconds: float = 10,
    headers: dict[str, str] | None = None,
) -> object:
    request: Request = Request(url, headers=headers or {})  # noqa: S310 - loopback test server only
    try:
        with urlopen(  # noqa: S310 - loopback test server only
            request, timeout=timeout_seconds
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(error.read().decode("utf-8")) from error


def post_json_url(
    url: str, payload: dict[str, object], *, headers: dict[str, str] | None = None
) -> object:
    request: Request = Request(  # noqa: S310 - loopback test server only
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - loopback test server only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(error.read().decode("utf-8")) from error


def prepare_lineage_browser_project(*, tmp_path: Path) -> Path:
    project_dir: Path = tmp_path / LINEAGE_BROWSER_PROJECT_DIR.name
    _ = copytree(LINEAGE_BROWSER_PROJECT_DIR, project_dir)
    return project_dir


def prepare_catalog_pipeline_browser_project(*, tmp_path: Path) -> Path:
    project_dir: Path = prepare_lineage_browser_project(tmp_path=tmp_path)
    (project_dir / "pipelines" / "pl__moving_events" / "derived_moving_orders.sql").write_text(
        'MODEL (\n  engine "MergeTree()",\n  order_by ["order_id"]\n);\n\n'
        "SELECT\n  order_id::String AS order_id,\n"
        "  _replay_timestamp::DateTime64(3) AS _replay_timestamp\n"
        'FROM __ref("moving_orders")\n',
        encoding="utf-8",
    )
    return project_dir


def create_lineage_browser_source_tables(*, client: Client, database: str) -> None:
    for relation_name in (
        "browser_moving_events",
        "browser_idle_events",
        "browser_stalled_events",
    ):
        client.command(
            f"CREATE TABLE {database}.{relation_name} "
            "(order_id String, event_timestamp DateTime64(3), "
            "_replay_partition Int64 DEFAULT 0, _replay_offset Int64 DEFAULT 0, "
            "_replay_landed_at DateTime64(3) DEFAULT now64(3)) "
            "ENGINE = MergeTree ORDER BY (event_timestamp, order_id)"
        )


def run_lineage_browser_build(
    *,
    repository_root: Path,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
) -> None:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            str(repository_root / ".venv" / "bin" / "sqt"),
            "build",
            "--project-dir",
            str(project_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--database",
            database,
            "--auto-approve",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"lineage browser build failed with code {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def seed_lineage_exact_activity(*, client: Client, database: str) -> None:
    client.command(
        f"INSERT INTO {database}.browser_moving_events (order_id, event_timestamp) "
        "VALUES ('101', now64(3))"
    )
    client.command(
        f"INSERT INTO {database}.browser_idle_events (order_id, event_timestamp) "
        "VALUES ('idle', now64(3))"
    )
    try:
        client.command(
            f"INSERT INTO {database}.browser_stalled_events (order_id, event_timestamp) "
            "VALUES ('not-a-number', now64(3))"
        )
    except Exception as error:
        assert "Cannot parse" in str(error) or "CANNOT_PARSE" in str(error)
    else:
        raise AssertionError("stalled activity probe unexpectedly succeeded")
    client.command("SYSTEM FLUSH LOGS")


def seed_lineage_approximate_activity(*, client: Client, database: str) -> None:
    client.command(
        f"INSERT INTO {database}.browser_moving_events (order_id, event_timestamp) "
        "VALUES ('101', now64(3))"
    )


def seed_lineage_plan_replay_data(*, client: Client, database: str) -> None:
    client.command(
        f"INSERT INTO {database}.browser_moving_events "
        "(order_id, event_timestamp, _replay_landed_at) VALUES "
        "('plan-oldest', now64(3) - INTERVAL 3 DAY, now64(3) - INTERVAL 3 DAY), "
        "('plan-middle', now64(3) - INTERVAL 2 DAY, now64(3) - INTERVAL 2 DAY), "
        "('plan-newest', now64(3) - INTERVAL 1 DAY, now64(3) - INTERVAL 1 DAY)"
    )


def start_dev_process(
    *,
    repository_root: Path,
    project_dir: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
    api_port: int,
    log_path: Path,
    extra_args: tuple[str, ...] = (),
) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        return subprocess.Popen(
            [
                str(repository_root / ".venv" / "bin" / "sqt"),
                "dev",
                "--project-dir",
                str(project_dir),
                "--host",
                host,
                "--port",
                str(port),
                "--username",
                username,
                "--password",
                password,
                "--database",
                database,
                "--ui-host",
                "127.0.0.1",
                "--ui-port",
                str(api_port),
                *extra_args,
            ],
            cwd=repository_root,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )


def wait_for_scheduler_api(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> dict[str, object]:
    deadline: float = time.monotonic() + 30
    while True:
        try:
            return cast(
                dict[str, object],
                read_json_url(f"http://127.0.0.1:{api_port}/api/audit-scheduler"),
            )
        except (OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before API readiness", process=process, log_path=log_path
        )
        assert time.monotonic() < deadline, _process_failure(
            message="sqt dev API did not become ready before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.1)


def wait_for_state_api(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> dict[str, object]:
    deadline: float = time.monotonic() + 30
    while True:
        try:
            payload: object = read_json_url(
                f"http://127.0.0.1:{api_port}/api/state", timeout_seconds=1
            )
            assert isinstance(payload, dict), "state payload is not an object"
            state_payload: dict[str, object] = cast(dict[str, object], payload)
            assert isinstance(state_payload.get("capturedAt"), str), (
                "state payload has no capturedAt timestamp"
            )
            assert isinstance(state_payload.get("models"), dict), (
                "state payload has no models object"
            )
            assert isinstance(state_payload.get("sources"), dict), (
                "state payload has no sources object"
            )
            return state_payload
        except (AssertionError, OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before state API readiness",
            process=process,
            log_path=log_path,
        )
        assert time.monotonic() < deadline, _process_failure(
            message="sqt dev state API did not become ready before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.1)


def wait_for_status_api(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> dict[str, object]:
    """Wait for the dev UI contract even when its warehouse is unavailable."""

    deadline: float = time.monotonic() + 30
    while True:
        try:
            payload: object = read_json_url(
                f"http://127.0.0.1:{api_port}/api/status", timeout_seconds=1
            )
            assert isinstance(payload, dict), "status payload is not an object"
            return cast(dict[str, object], payload)
        except (AssertionError, OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before status API readiness",
            process=process,
            log_path=log_path,
        )
        assert time.monotonic() < deadline, _process_failure(
            message="sqt dev status API did not become ready before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.1)


def wait_for_authenticated_status_api(
    *,
    process: subprocess.Popen[str],
    api_port: int,
    log_path: Path,
    username: str,
) -> dict[str, object]:
    deadline: float = time.monotonic() + 30
    while True:
        try:
            payload: object = read_json_url(
                f"http://127.0.0.1:{api_port}/api/status",
                timeout_seconds=1,
                headers={"X-SQLTide-User": username},
            )
            assert isinstance(payload, dict), "status payload is not an object"
            status_payload: dict[str, object] = cast(dict[str, object], payload)
            compile_payload: object = status_payload.get("compile")
            assert isinstance(compile_payload, dict), "status payload has no compile object"
            compile_state: object = cast(dict[str, object], compile_payload).get("state")
            assert compile_state == "ok", "project compile is not servable"
            return status_payload
        except (AssertionError, OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before status API readiness",
            process=process,
            log_path=log_path,
        )
        assert time.monotonic() < deadline, _process_failure(
            message="sqt dev status API did not become ready before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.1)


def wait_for_auth_config_api(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> dict[str, object]:
    deadline: float = time.monotonic() + 30
    while True:
        try:
            payload: object = read_json_url(
                f"http://127.0.0.1:{api_port}/api/auth/config", timeout_seconds=1
            )
            assert isinstance(payload, dict), "auth config payload is not an object"
            return cast(dict[str, object], payload)
        except (AssertionError, OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before auth API readiness", process=process, log_path=log_path
        )
        assert time.monotonic() < deadline, _process_failure(
            message="sqt dev auth API did not become ready before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.1)


def prepare_authorization_browser_project(*, tmp_path: Path) -> Path:
    project_dir: Path = prepare_lineage_browser_project(tmp_path=tmp_path)
    (project_dir / "access.yml").write_text(
        "roles:\n"
        "  reload_operator:\n"
        "    description: Reload the project\n"
        "    grants:\n"
        "      - scope: project\n"
        "        permissions: [project.reload]\n"
        "  moving_operator:\n"
        "    description: Operate the moving events pipeline\n"
        "    grants:\n"
        "      - pipelines: [pl__moving_events]\n"
        "        permissions: [quality.audit.run, build.direct.run]\n",
        encoding="utf-8",
    )
    return project_dir


def provision_authorization_accounts(*, control_store_url: str, project_name: str) -> None:
    """Create the admin/operator/viewer/stale/mismatch personas for browser tests."""

    store: ControlStore = ControlStore(url=control_store_url)
    try:
        store.create_user(
            username="kevin",
            authentication_source=AuthenticationSource.TRUSTED_PROXY,
            external_subject="kevin",
            roles=("admin",),
        )
        alice: UserAccount = store.create_user(
            username="alice",
            authentication_source=AuthenticationSource.TRUSTED_PROXY,
            external_subject="alice",
            roles=("viewer",),
        )
        for role_name in ("reload_operator", "moving_operator"):
            store.grant_project_role(
                user_id=alice.user_id,
                project_name=project_name,
                role_name=role_name,
                target_name=None,
                actor_user_id=None,
            )
        assignments: tuple[tuple[str, str, str | None], ...] = (
            ("carol", "retired_role", None),
            ("dave", "reload_operator", "prod"),
        )
        for username, role_name, target_name in assignments:
            account: UserAccount = store.create_user(
                username=username,
                authentication_source=AuthenticationSource.TRUSTED_PROXY,
                external_subject=username,
                roles=("viewer",),
            )
            store.grant_project_role(
                user_id=account.user_id,
                project_name=project_name,
                role_name=role_name,
                target_name=target_name,
                actor_user_id=None,
            )
        store.create_user(
            username="bob",
            authentication_source=AuthenticationSource.TRUSTED_PROXY,
            external_subject="bob",
            roles=("viewer",),
        )
    finally:
        store.close()


def provision_password_account(*, control_store_url: str, username: str, password: str) -> None:
    store: ControlStore = ControlStore(url=control_store_url)
    try:
        store.create_user(username=username, password=password, roles=("admin",))
    finally:
        store.close()


def browser_post_reload(*, page: Page) -> dict[str, object]:
    """POST /api/reload from the authenticated page and summarize the decision."""

    script: str = (
        "async () => {"
        "  const response = await fetch('/api/reload', {"
        "    method: 'POST',"
        "    headers: { 'X-SQLTide-CSRF': 'trusted-proxy' }"
        "  });"
        "  const body = await response.json().catch(() => ({}));"
        "  return {"
        "    status: response.status,"
        "    reason: body?.detail?.reason ?? null,"
        "    permission: body?.detail?.permission ?? null,"
        "    compileState: body?.compile?.state ?? null"
        "  };"
        "}"
    )
    return cast(dict[str, object], page.evaluate(script))


def wait_for_scheduled_result(
    *,
    processes: tuple[subprocess.Popen[str], ...],
    client: Client,
    database: str,
    expected_count: int = 1,
) -> None:
    deadline: float = time.monotonic() + 30
    result_count = 0
    while result_count < expected_count:
        assert time.monotonic() < deadline, "scheduled result did not arrive before timeout"
        assert all(process.poll() is None for process in processes), (
            f"sqt dev exited before scheduling completed: "
            f"{tuple(process.returncode for process in processes)}"
        )
        result_count = int(
            client.query(
                "SELECT coalesce(sum(rows), 0) FROM system.parts "
                f"WHERE database = '{database}' "
                "AND table = '_sqltide_node_results' AND active = 1"
            ).result_rows[0][0]
        )
        time.sleep(0.1)
    assert result_count == expected_count


_SENSORS_BROWSER_SENSOR_SOURCE: str = '''
from sqltide.events import AuditCompleted
from sqltide.sensors import DefaultSensorStatus, SensorRetryPolicy, event_sensor


@event_sensor(
    on=AuditCompleted,
    default_status=DefaultSensorStatus.RUNNING,
    retry_policy=SensorRetryPolicy(max_attempts=1, backoff_seconds=0),
)
def flaky_alerts(ctx):
    """Alert on audit completions; the demo webhook always fails."""

    raise RuntimeError("simulated alert delivery failure")


@event_sensor(on=AuditCompleted)
def paused_watch(ctx):
    """Stopped by default until an operator starts it."""
'''


def append_sensors_browser_automation(*, project_dir: Path) -> None:
    """Enable the sensor dispatcher and author the browser test sensors."""

    toml_path: Path = project_dir / "sqltide_project.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8") + "\n[targets.test.sensors]\nenabled = true\n",
        encoding="utf-8",
    )
    sensors_dir: Path = project_dir / "sensors"
    sensors_dir.mkdir(parents=True, exist_ok=True)
    (sensors_dir / "alerts.py").write_text(
        _SENSORS_BROWSER_SENSOR_SOURCE.strip() + "\n", encoding="utf-8"
    )


def wait_for_sensor_first_dispatch(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> None:
    """Wait until the dispatcher holds the lease and has completed one pass."""

    deadline: float = time.monotonic() + 60
    while True:
        try:
            payload: dict[str, object] = cast(
                dict[str, object], read_json_url(f"http://127.0.0.1:{api_port}/api/sensors")
            )
            health: dict[str, object] = cast(dict[str, object], payload["health"])
            if health["leaseHeld"] is True and health["lastSuccessfulTick"] is not None:
                return
        except (OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before the first sensor dispatch",
            process=process,
            log_path=log_path,
        )
        assert time.monotonic() < deadline, _process_failure(
            message="the sensor dispatcher never completed a pass before timeout",
            process=process,
            log_path=log_path,
        )
        time.sleep(0.5)


def wait_for_sensor_dead_letter(
    *, process: subprocess.Popen[str], api_port: int, log_path: Path
) -> dict[str, object]:
    """Wait until the dispatcher records one unresolved dead letter."""

    deadline: float = time.monotonic() + 90
    while True:
        try:
            payload: object = read_json_url(f"http://127.0.0.1:{api_port}/api/sensors/dead-letters")
            letters: object = cast(dict[str, object], payload)["deadLetters"]
            if isinstance(letters, list) and letters:
                return cast(dict[str, object], letters[0])
        except (OSError, RuntimeError):
            pass
        assert process.poll() is None, _process_failure(
            message="sqt dev exited before a dead letter was recorded",
            process=process,
            log_path=log_path,
        )
        assert time.monotonic() < deadline, _process_failure(
            message=(
                "no sensor dead letter was recorded before timeout; sensors payload: "
                f"{_best_effort_sensors_payload(api_port=api_port)}"
            ),
            process=process,
            log_path=log_path,
        )
        time.sleep(0.5)


def _best_effort_sensors_payload(*, api_port: int) -> str:
    try:
        return json.dumps(read_json_url(f"http://127.0.0.1:{api_port}/api/sensors"))
    except (OSError, RuntimeError) as error:
        return f"<unavailable: {error}>"


def stop_process(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _process_failure(*, message: str, process: subprocess.Popen[str], log_path: Path) -> str:
    try:
        log_contents: str = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log_contents = "<log file was not created>"
    return (
        f"{message}: returncode={process.poll()}, log={log_path}\n"
        f"--- sqt dev output ---\n{log_contents}"
    )


def available_port() -> int:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        return int(port_socket.getsockname()[1])


SOURCE_RECREATION_MESSAGE: str = (
    "kafka__orders and mv__orders are recreated with the declared settings (kafka_topic_list); "
    "raw__orders and its rows are kept."
)
SOURCE_RECREATION_SQL_DIFF: str = (
    "--- previous\n+++ current\n@@ -1,2 +1,2 @@\n SELECT order_id\n-FROM orders\n"
    "+FROM orders WHERE order_id > 0\n"
)
SOURCE_RECREATION: dict[str, object] = {
    "sourceName": "orders",
    "relationName": "kafka__orders",
    "landingViewName": "mv__orders",
    "landingRelationName": "raw__orders",
    "stagedRelationName": "_sqltide_next__kafka__orders",
    "consumerGroup": "sqltide_orders_analytics",
    "driftedSettings": ["kafka_topic_list", "kafka_max_block_size"],
    "message": SOURCE_RECREATION_MESSAGE,
    "selectedPipeline": "pl__moving_events",
    "otherReaderPipelines": ["pl__order_audit"],
}


def build_source_recreation_plan_payload(
    *, source_recreations: list[dict[str, object]]
) -> dict[str, object]:
    """Serve a direct changed plan whose owning pipeline is rebuilt for a drifted source."""

    return {
        "mode": "direct",
        "database": "analytics",
        "userScope": [],
        "entries": [
            {
                "modelName": "moving_orders",
                "pipeline": "pl__moving_events",
                "reason": "source_drift",
                "relationNames": ["tbl__moving_orders"],
                "resourceKinds": ["table"],
                "drivingInput": None,
                "isReplayRoot": False,
                "sqlChange": {
                    "status": "query_changed",
                    "unifiedDiff": SOURCE_RECREATION_SQL_DIFF,
                    "warning": None,
                },
            }
        ],
        "prerequisites": [],
        "teardown": [],
        "creation": [],
        "replayRoots": [],
        "warnings": [
            {
                "code": "managed_source_recreated",
                "message": SOURCE_RECREATION_MESSAGE,
                "relatedModel": None,
            }
        ],
        "sourceRecreations": source_recreations,
        "protections": [],
        "replayWindow": {"mode": "full"},
        "plannedAt": "2026-08-01T12:00:00Z",
        "command": "sqt build --changed",
        "executionOrder": ["direct"],
        "phases": [
            {
                "mode": "direct",
                "effect": "applied_immediately",
                "deploymentId": None,
                "modelNames": ["moving_orders"],
                "contextModelNames": [],
                "relationNames": ["tbl__moving_orders"],
                "actions": [],
                "startTime": None,
            }
        ],
        "deploymentId": None,
        "upperBoundary": {"mode": "captured_at_execution", "continuesLive": True},
    }


def run_slow_reload_dev_server(arguments: list[str]) -> None:
    """Serve an offline dev app whose reloads compile slowly, then report a busy thread's progress.

    Arguments: project directory, port, reload delay in seconds, report path. The first compile
    is immediate and every later one sleeps for the delay. The busy thread counts iterations
    before serving and while serving; the server must leave it the interpreter lock while it
    waits for connections and while a handler sleeps.
    """

    project_dir, port, delay, report_path = arguments
    write_dev_server_project(project_dir=Path(project_dir))
    compile_project: Callable[[], CompileAnalysis] = build_compile_callable(
        project_dir=Path(project_dir)
    )
    compiles: itertools.count[int] = itertools.count()

    def slow_compile() -> CompileAnalysis:
        time.sleep(float(delay) * min(next(compiles), 1))
        return compile_project()

    app: DevApplication = create_dev_app(
        state=DevServerState(run_compile=slow_compile), project_dir=Path(project_dir)
    )
    ticks: itertools.count[int] = itertools.count()
    stop: threading.Event = threading.Event()
    busy: threading.Thread = threading.Thread(
        target=lambda: deque(iter(lambda: (next(ticks), stop.is_set())[1], True), maxlen=0)
    )
    busy.start()
    time.sleep(_BUSY_BASELINE_SECONDS)
    baseline: int = next(ticks)
    started: float = time.monotonic()
    app.serve("127.0.0.1", int(port))
    serving_seconds: float = time.monotonic() - started
    serving: int = next(ticks) - baseline
    stop.set()
    busy.join()
    Path(report_path).write_text(
        json.dumps(
            {
                "baselineRate": baseline / _BUSY_BASELINE_SECONDS,
                "servingRate": serving / serving_seconds,
            }
        ),
        encoding="utf-8",
    )


def start_slow_reload_dev_server(
    *,
    repository_root: Path,
    project_dir: Path,
    port: int,
    delay_seconds: float,
    report_path: Path,
    log_path: Path,
) -> subprocess.Popen[str]:
    with log_path.open("w", encoding="utf-8") as log_file:
        return subprocess.Popen(
            [
                str(repository_root / ".venv" / "bin" / "python"),
                "-c",
                "import sys; from tests.e2e.src.sqltide.dev_server.helpers import "
                "run_slow_reload_dev_server; run_slow_reload_dev_server(sys.argv[1:])",
                str(project_dir),
                str(port),
                str(delay_seconds),
                str(report_path),
            ],
            cwd=repository_root,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )


def timed_status(url: str, method: str = "GET") -> tuple[int, float]:
    """The status of one request and how long it took."""

    request: Request = Request(  # noqa: S310 - loopback test server only
        url, method=method, headers={"X-SQLTide-CSRF": "trusted-proxy"}
    )
    started: float = time.monotonic()
    with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback test server only
        response.read()
        return int(response.status), time.monotonic() - started


_BUSY_BASELINE_SECONDS: float = 0.5
