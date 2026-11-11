import subprocess
import sys

import pytest

from tests.unit.src.sqltide.cli._test_types import UnreachableClickHouseStderrTestCase
from tests.unit.src.sqltide.cli.helpers import closed_local_port, default_clickhouse_port

_CHECKS: str = (
    "Database: analytics\n"
    "\n"
    "Check:\n"
    "- that ClickHouse is running and reachable from this machine\n"
    "- the host and port in your connection settings\n"
)
_DEBUG_PRELUDE: str = (
    "import logging; handler = logging.StreamHandler(); "
    "handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s')); "
    "driver = logging.getLogger('clickhouse_connect.driver.httpclient'); "
    "driver.addHandler(handler); driver.setLevel(logging.DEBUG); "
)


@pytest.mark.parametrize(
    "test_case",
    [
        UnreachableClickHouseStderrTestCase(
            description="reports a refused connection once, without the driver log line",
            host="127.0.0.1",
            port=closed_local_port,
            logging_prelude="",
            expected_stderr=(
                "Plan could not connect to ClickHouse at 127.0.0.1:{port}: "
                "connection refused\n" + _CHECKS
            ),
        ),
        UnreachableClickHouseStderrTestCase(
            description="reports an unresolvable host once, without the driver log line",
            host="clickhouse.invalid",
            port=default_clickhouse_port,
            logging_prelude="",
            expected_stderr=(
                "Plan could not connect to ClickHouse at clickhouse.invalid:{port}: "
                "host not found\n" + _CHECKS
            ),
        ),
        UnreachableClickHouseStderrTestCase(
            description="keeps the driver line at debug level when debug logging is on",
            host="127.0.0.1",
            port=closed_local_port,
            logging_prelude=_DEBUG_PRELUDE,
            expected_stderr=(
                "DEBUG:clickhouse_connect.driver.httpclient:Unexpected Http Driver Exception\n"
                "Plan could not connect to ClickHouse at 127.0.0.1:{port}: "
                "connection refused\n" + _CHECKS
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_unreachable_clickhouse_when_planning_then_stderr_is_exactly_the_cli_error(
    test_case: UnreachableClickHouseStderrTestCase,
) -> None:
    port: int = test_case.port()

    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            test_case.logging_prelude
            + "import sys; from sqltide._native import run_cli; sys.exit(run_cli(sys.argv[1:]))",
            "sqt",
            "plan",
            "--project-dir",
            "tests/fixtures/basic_project",
            "--host",
            test_case.host,
            "--port",
            str(port),
            "--username",
            "sqltide",
            "--password",
            "sqltide",
            "--database",
            "analytics",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert (completed.returncode, completed.stderr) == (
        1,
        test_case.expected_stderr.format(port=port),
    )
