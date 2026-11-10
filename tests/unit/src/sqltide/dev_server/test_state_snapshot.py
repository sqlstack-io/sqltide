import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from sqltide._native import OverlayReader, StateSnapshot
from sqltide.compiler.pipeline.models import CompileAnalysis
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import NativeTestClient
from tests.unit.src.sqltide.dev_server._test_types import (
    OverlayReaderTestCase,
    StateSnapshotTestCase,
    WarehouseRefreshSnapshotTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    FakeAdapterConnection,
    build_compile_callable,
    build_overlay_reader,
    build_snapshot_counting_client,
    failing_state_build,
    recording_state_build,
    sequenced_state_build,
)


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="serves the held overlay to every later request",
            request_count=5,
            expected_build_count=1,
        ),
        StateSnapshotTestCase(
            description="builds once for a single request",
            request_count=1,
            expected_build_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_repeated_requests_when_reading_snapshot_then_one_build_is_reused(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(build=recording_state_build(calls))

    _ = [snapshot.current() for _ in range(test_case.request_count)]

    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="reading an empty held snapshot never triggers its builder",
            request_count=1,
            expected_build_count=0,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_empty_snapshot_when_reading_held_value_then_no_warehouse_build_runs(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(build=recording_state_build(calls))

    held: dict[str, object] | None = snapshot.held()

    assert held is None
    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="rebuilds after invalidation so new definitions are picked up",
            request_count=2,
            expected_build_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_invalidated_snapshot_when_reading_then_it_rebuilds(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(build=recording_state_build(calls))

    _ = snapshot.current()
    snapshot.invalidate()
    reread: dict[str, object] = snapshot.current()

    assert reread["capturedAt"] == "build-2"
    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="a forced refresh replaces the held overlay",
            request_count=1,
            expected_build_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_forced_refresh_when_reading_snapshot_then_latest_overlay_is_served(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(build=recording_state_build(calls))

    _ = snapshot.current()
    refreshed: dict[str, object] = snapshot.refresh()

    assert refreshed["capturedAt"] == "build-2"
    assert snapshot.current()["capturedAt"] == "build-2"
    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="concurrent refreshes serialize warehouse builds",
            request_count=2,
            expected_build_count=2,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_refresh_in_progress_when_another_starts_then_builds_do_not_overlap(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    first_started: threading.Event = threading.Event()
    release_first: threading.Event = threading.Event()
    second_started: threading.Event = threading.Event()

    def first_build() -> dict[str, object]:
        calls.append("build-1")
        first_started.set()
        _ = release_first.wait(timeout=1.0)
        return {"capturedAt": calls[-1]}

    def second_build() -> dict[str, object]:
        calls.append("build-2")
        second_started.set()
        return {"capturedAt": calls[-1]}

    snapshot: StateSnapshot = StateSnapshot(
        build=sequenced_state_build([first_build, second_build])
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first: Future[dict[str, object]] = executor.submit(snapshot.refresh)
        assert first_started.wait(timeout=1.0)
        second: Future[dict[str, object]] = executor.submit(snapshot.refresh)
        assert not second_started.wait(timeout=0.05)
        release_first.set()
        _ = first.result(timeout=1.0)
        _ = second.result(timeout=1.0)

    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="a failing first build propagates instead of serving nothing",
            request_count=1,
            expected_build_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failing_first_build_when_reading_snapshot_then_error_propagates(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(build=failing_state_build(calls))

    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        _ = snapshot.current()

    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="a failing refresh keeps serving the last good overlay",
            request_count=1,
            expected_build_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_failing_refresh_when_reading_snapshot_then_last_good_overlay_survives(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(
        build=sequenced_state_build([recording_state_build(calls), failing_state_build(calls)])
    )
    _ = snapshot.current()

    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        _ = snapshot.refresh()

    assert snapshot.current()["capturedAt"] == "build-1"
    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseRefreshSnapshotTestCase(
            description="an explicit warehouse refresh discards the held overlay",
            refresh_count=1,
            expected_build_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_explicit_warehouse_refresh_when_reading_state_then_overlay_is_rebuilt(
    test_case: WarehouseRefreshSnapshotTestCase,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    client: NativeTestClient = build_snapshot_counting_client(project_dir=tmp_path, calls=calls)
    _ = client.get("/api/state")

    _ = [client.post("/api/warehouse/refresh") for _ in range(test_case.refresh_count)]
    _ = client.get("/api/state")

    assert len(calls) == test_case.expected_build_count


@pytest.mark.parametrize(
    "test_case",
    [
        OverlayReaderTestCase(
            description="repeated overlay reads reuse one private connection",
            read_count=3,
            expected_connection_count=1,
        ),
        OverlayReaderTestCase(
            description="a single overlay read opens one private connection",
            read_count=1,
            expected_connection_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_repeated_overlay_reads_when_building_then_one_connection_is_reused(
    test_case: OverlayReaderTestCase,
    tmp_path: Path,
) -> None:
    connections: list[FakeAdapterConnection] = []
    reader: OverlayReader = build_overlay_reader(project_dir=tmp_path, connections=connections)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()

    _ = [reader.read(analysis=analysis, database="analytics") for _ in range(test_case.read_count)]

    assert len(connections) == test_case.expected_connection_count


@pytest.mark.parametrize(
    "test_case",
    [
        OverlayReaderTestCase(
            description="closing the reader reconnects on the next overlay read",
            read_count=1,
            expected_connection_count=2,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_closed_overlay_reader_when_reading_again_then_a_new_connection_opens(
    test_case: OverlayReaderTestCase,
    tmp_path: Path,
) -> None:
    connections: list[FakeAdapterConnection] = []
    reader: OverlayReader = build_overlay_reader(project_dir=tmp_path, connections=connections)
    analysis: CompileAnalysis = build_compile_callable(project_dir=tmp_path)()

    _ = reader.read(analysis=analysis, database="analytics")
    reader.close()
    _ = reader.read(analysis=analysis, database="analytics")

    assert len(connections) == test_case.expected_connection_count


@pytest.mark.parametrize(
    "test_case",
    [
        StateSnapshotTestCase(
            description="closing the snapshot releases the connection it owns",
            request_count=1,
            expected_build_count=1,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_snapshot_owning_a_connection_when_closed_then_it_is_released(
    test_case: StateSnapshotTestCase,
) -> None:
    calls: list[str] = []
    released: list[str] = []
    snapshot: StateSnapshot = StateSnapshot(
        build=recording_state_build(calls),
        on_close=lambda: released.append("released"),
    )

    _ = snapshot.current()
    snapshot.close()

    assert released == ["released"]
    assert len(calls) == test_case.expected_build_count
