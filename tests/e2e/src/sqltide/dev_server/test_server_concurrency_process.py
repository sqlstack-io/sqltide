import json
import signal
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.src.sqltide.dev_server._test_types import DevServerConcurrencyE2ETestCase
from tests.e2e.src.sqltide.dev_server.helpers import (
    available_port,
    start_slow_reload_dev_server,
    timed_status,
    wait_for_auth_config_api,
)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "test_case",
    [
        DevServerConcurrencyE2ETestCase(
            description="slow handler does not hold up others or the interpreter",
            slow_seconds=2.0,
            fast_requests=24,
            expected_result=(0, True, 200, True, frozenset({200}), True, True),
            expected_fast_budget_seconds=1.0,
            expected_minimum_busy_ratio=0.2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_slow_handler_when_requests_run_in_parallel_then_others_finish_and_gil_is_free(
    test_case: DevServerConcurrencyE2ETestCase, tmp_path: Path
) -> None:
    port: int = available_port()
    report_path: Path = tmp_path / "report.json"
    log_path: Path = tmp_path / "server.log"
    process: subprocess.Popen[str] = start_slow_reload_dev_server(
        repository_root=Path.cwd(),
        project_dir=tmp_path / "project",
        port=port,
        delay_seconds=test_case.slow_seconds,
        report_path=report_path,
        log_path=log_path,
    )
    wait_for_auth_config_api(process=process, api_port=port, log_path=log_path)
    base: str = f"http://127.0.0.1:{port}"

    with ThreadPoolExecutor(max_workers=test_case.fast_requests + 1) as pool:
        slow: Future[tuple[int, float]] = pool.submit(timed_status, f"{base}/api/reload", "POST")
        fast: list[tuple[int, float]] = list(
            pool.map(timed_status, [f"{base}/api/auth/config"] * test_case.fast_requests)
        )
        overlapped: bool = not slow.done()
        slow_result: tuple[int, float] = slow.result()
    process.send_signal(signal.SIGINT)
    exit_code: int = process.wait(timeout=30)
    report: dict[str, float] = json.loads(report_path.read_text(encoding="utf-8"))

    assert (
        exit_code,
        overlapped,
        slow_result[0],
        slow_result[1] >= test_case.slow_seconds,
        frozenset(status for status, _ in fast),
        max(elapsed for _, elapsed in fast) < test_case.expected_fast_budget_seconds,
        report["servingRate"] >= test_case.expected_minimum_busy_ratio * report["baselineRate"],
    ) == test_case.expected_result, log_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
