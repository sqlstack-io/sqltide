from pathlib import Path

import pytest

from sqltide._native import static_assets_present
from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    SpaFallbackTestCase,
    StaticAssetsPresenceTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_static_test_client,
    write_static_assets_build,
)


@pytest.mark.parametrize(
    "test_case",
    [
        StaticAssetsPresenceTestCase(
            description="a built UI with an index shell is present",
            expected_present=True,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_built_assets_when_checking_presence_then_reports_present(
    test_case: StaticAssetsPresenceTestCase,
    tmp_path: Path,
) -> None:
    write_static_assets_build(assets_root=tmp_path)

    assert static_assets_present(assets_root=tmp_path) is test_case.expected_present


@pytest.mark.parametrize(
    "test_case",
    [
        StaticAssetsPresenceTestCase(
            description="a checkout without a built UI is not present",
            expected_present=False,
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_empty_assets_root_when_checking_presence_then_reports_absent(
    test_case: StaticAssetsPresenceTestCase,
    tmp_path: Path,
) -> None:
    assert static_assets_present(assets_root=tmp_path) is test_case.expected_present


@pytest.mark.parametrize(
    "test_case",
    [
        SpaFallbackTestCase(
            description="root path serves the SPA shell",
            request_path="/",
            expected_body_fragment="sqt-dev-shell",
            expected_cache_control="no-cache, must-revalidate",
        ),
        SpaFallbackTestCase(
            description="deep link falls back to the SPA shell",
            request_path="/lineage",
            expected_body_fragment="sqt-dev-shell",
            expected_cache_control="no-cache, must-revalidate",
        ),
        SpaFallbackTestCase(
            description="non-immutable app asset is revalidated",
            request_path="/_app/app.js",
            expected_body_fragment="sqt-app-script",
            expected_cache_control="no-cache, must-revalidate",
        ),
        SpaFallbackTestCase(
            description="version metadata is revalidated",
            request_path="/_app/version.json",
            expected_body_fragment="example-version",
            expected_cache_control="no-cache, must-revalidate",
        ),
        SpaFallbackTestCase(
            description="content-hashed app asset is cached immutably",
            request_path="/_app/immutable/app.example.js",
            expected_body_fragment="sqt-immutable-script",
            expected_cache_control="public, max-age=31536000, immutable",
        ),
        SpaFallbackTestCase(
            description="real top-level file is served verbatim",
            request_path="/robots.txt",
            expected_body_fragment="User-agent",
            expected_cache_control="no-cache, must-revalidate",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_built_assets_when_requesting_path_then_serves_expected_body(
    test_case: SpaFallbackTestCase,
    tmp_path: Path,
) -> None:
    write_static_assets_build(assets_root=tmp_path)
    client: NativeTestClient = build_static_test_client(assets_root=tmp_path)

    response: NativeResponse = client.get(test_case.request_path)

    assert response.status_code == 200
    assert test_case.expected_body_fragment in response.text
    assert response.headers["cache-control"] == test_case.expected_cache_control
