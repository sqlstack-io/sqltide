from pathlib import Path
from typing import cast

import pytest
from clickhouse_connect.driver.client import Client
from playwright.sync_api import ConsoleMessage, Error, Page, Request, Response, expect

from tests.e2e.src.sqltide.dev_server._test_types import (
    KafkaNotBuiltBrowserE2ETestCase,
    WarehouseHealthBrowserE2ETestCase,
)


@pytest.mark.e2e
@pytest.mark.browser
@pytest.mark.parametrize(
    "test_case",
    [
        WarehouseHealthBrowserE2ETestCase(
            description="overview and drill-down show one truthful warehouse snapshot",
            expected_adapter="clickhouse",
            expected_capacity_heading="Capacity",
            expected_table_heading="Largest project tables",
            expected_freshness_state="Not configured",
            expected_test_state="Not run on test",
            expected_header_font_size="10px",
            expected_header_font_weight="400",
            expected_body_font_size="11.5px",
            expected_body_font_weight="400",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_available_warehouse_health_when_navigating_ui_then_summary_and_detail_render(
    test_case: WarehouseHealthBrowserE2ETestCase,
    running_lineage_server: tuple[str, dict[str, object], Path, Client, str],
    browser_diagnostics: tuple[list[ConsoleMessage], list[Error], list[Request], list[Response]],
    page: Page,
) -> None:
    base_url, initial_state, _log_path, _clickhouse_client, database = running_lineage_server
    _console_messages, page_errors, failed_requests, _responses = browser_diagnostics
    health: dict[str, object] = cast(dict[str, object], initial_state["warehouseHealth"])

    assert health["adapter"] == test_case.expected_adapter
    assert health["availability"] in {"available", "partial"}
    assert health["status"] in {"healthy", "warning", "critical"}
    assert cast(list[object], health["disks"])
    assert cast(list[object], health["tables"])

    assert page.goto(base_url, wait_until="networkidle") is not None
    expect(page.get_by_test_id("warehouse-health-summary")).to_be_visible()
    expect(page.get_by_text(test_case.expected_freshness_state, exact=True)).to_be_visible()
    expect(page.get_by_test_id("quality-tests-summary")).to_contain_text(
        test_case.expected_test_state
    )
    expect(page.get_by_text("unknown", exact=True)).to_have_count(0)
    expect(page.get_by_text("Current bounded warehouse snapshot.", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Warehouse", exact=True).click()

    expect(page).to_have_url(f"{base_url}/warehouse-health")
    expect(page.get_by_test_id("warehouse-health-page")).to_be_visible()
    expect(page.get_by_text(test_case.expected_capacity_heading, exact=True)).to_be_visible()
    expect(page.get_by_text(test_case.expected_table_heading, exact=False).first).to_be_visible()
    expect(page.get_by_text(database, exact=True).first).to_be_visible()
    expect(page.get_by_role("columnheader", name="Active parts", exact=True)).to_be_visible()
    header_style: dict[str, str] = cast(
        dict[str, str],
        page.get_by_role("columnheader", name="Table", exact=True).evaluate(
            "element => ({ fontSize: getComputedStyle(element).fontSize, "
            "fontWeight: getComputedStyle(element).fontWeight })"
        ),
    )
    body_style: dict[str, str] = cast(
        dict[str, str],
        page.locator("table.sb-list tbody td").first.evaluate(
            "element => ({ fontSize: getComputedStyle(element).fontSize, "
            "fontWeight: getComputedStyle(element).fontWeight })"
        ),
    )
    assert header_style == {
        "fontSize": test_case.expected_header_font_size,
        "fontWeight": test_case.expected_header_font_weight,
    }
    assert body_style == {
        "fontSize": test_case.expected_body_font_size,
        "fontWeight": test_case.expected_body_font_weight,
    }
    assert page_errors == []
    assert failed_requests == []


@pytest.mark.e2e
@pytest.mark.browser
@pytest.mark.parametrize(
    "test_case",
    [
        KafkaNotBuiltBrowserE2ETestCase(
            description="authored Kafka source added after build remains neutral",
            expected_not_built_text="1 not built",
            expected_live_polling_text="1/1 live polling",
            excluded_error_text="Error",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unbuilt_kafka_source_when_viewing_overview_then_ingest_is_not_an_error(
    test_case: KafkaNotBuiltBrowserE2ETestCase,
    running_partially_built_kafka_server: tuple[str, dict[str, object]],
    browser_diagnostics: tuple[list[ConsoleMessage], list[Error], list[Request], list[Response]],
    page: Page,
) -> None:
    base_url, initial_state = running_partially_built_kafka_server
    _console_messages, page_errors, failed_requests, _responses = browser_diagnostics
    health: dict[str, object] = cast(dict[str, object], initial_state["warehouseHealth"])
    consumers: dict[str, object] = cast(dict[str, object], health["kafkaConsumers"])

    assert consumers == {
        "configuredTables": 2,
        "materializedTables": 1,
        "materializedTableNames": ["kafka__built_events"],
        "pollingTables": 1,
        "exceptionTables": 0,
        "stuckConsumers": [],
    }
    assert page.goto(base_url, wait_until="networkidle") is not None
    expect(page.get_by_text(test_case.expected_not_built_text, exact=True)).to_be_visible()
    expect(page.get_by_text(test_case.expected_live_polling_text, exact=True)).to_be_visible()
    expect(page.get_by_text(test_case.excluded_error_text, exact=True)).to_have_count(0)
    assert page_errors == []
    assert failed_requests == []


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])
