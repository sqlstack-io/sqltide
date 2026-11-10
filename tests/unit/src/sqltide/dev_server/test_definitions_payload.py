from pathlib import Path

import pytest

from tests.unit.src.sqltide.dev_server._helpers.server.helpers import (
    NativeResponse,
    NativeTestClient,
)
from tests.unit.src.sqltide.dev_server._test_types import (
    ConnectionSettingsPayloadTestCase,
    DefinitionsFieldTestCase,
    DependencyPolicyPayloadTestCase,
    DevRefactorTestCase,
)
from tests.unit.src.sqltide.dev_server.helpers import (
    build_test_client,
    named_payload_item,
    write_connection_settings_project,
    write_dev_server_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        DefinitionsFieldTestCase(
            description="serializes model, source, and audit definitions",
            expected_model_name="orders_clean",
            expected_model_description="Cleaned order rows.",
            expected_column_description="Primary order id",
            expected_anchor="eligible",
            expected_audit_name="orders_clean.order_id.not_null.1",
            expected_audit_file_suffix="orders_clean.sql",
            expected_audit_generic_name="not_null",
            expected_driving_input="orders",
            expected_source_kind="kafka",
            expected_managed_ddl_fragment="CREATE",
            expected_model_reference_scope="project",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_compiled_project_when_reading_definitions_then_serializes_expected_fields(
    test_case: DefinitionsFieldTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    payload: dict = client.get("/api/definitions").json()

    model: dict = named_payload_item(payload["models"], test_case.expected_model_name)
    assert model["description"] == test_case.expected_model_description
    assert model["drivingInput"] == test_case.expected_driving_input
    assert model["anchor"] == test_case.expected_anchor
    column: dict = named_payload_item(model["columns"], "order_id")
    assert column["description"] == test_case.expected_column_description
    assert model["sql"]["authored"].startswith("MODEL (")
    assert model["sql"]["compiled"]
    assert model["sql"]["ddl"]["table"]
    audit: dict = named_payload_item(payload["audits"], test_case.expected_audit_name)
    assert audit["file"].endswith(test_case.expected_audit_file_suffix)
    assert audit["genericName"] == test_case.expected_audit_generic_name
    source: dict = named_payload_item(payload["sources"], "orders")
    assert source["kind"] == test_case.expected_source_kind
    assert all(
        test_case.expected_managed_ddl_fragment in relation["ddl"]
        for relation in source["managedRelations"]
    )
    assert payload["project"]["database"] == "analytics"
    assert payload["project"]["dependencies"]["modelReferenceScope"] == (
        test_case.expected_model_reference_scope
    )
    assert tuple(payload["project"]["dependencies"]["allowedCrossPipelineReferences"]) == (
        test_case.expected_allowed_cross_pipeline_references
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DependencyPolicyPayloadTestCase(
            description="definitions and bootstrap expose explicit pipeline reference scope",
            project_config_suffix=('\n[dependencies]\nmodel_reference_scope = "pipeline"\n'),
            expected_model_reference_scope="pipeline",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_dependency_scope_when_reading_definitions_then_all_payloads_agree(
    test_case: DependencyPolicyPayloadTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    project_config_path: Path = tmp_path / "sqltide_project.toml"
    project_config_path.write_text(
        project_config_path.read_text(encoding="utf-8") + test_case.project_config_suffix,
        encoding="utf-8",
    )
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    definitions: dict = client.get("/api/definitions").json()
    bootstrap: dict = client.get("/api/bootstrap").json()

    assert definitions["project"]["dependencies"]["modelReferenceScope"] == (
        test_case.expected_model_reference_scope
    )
    assert (
        tuple(definitions["project"]["dependencies"]["allowedCrossPipelineReferences"])
        == test_case.expected_allowed_cross_pipeline_references
    )
    assert (
        bootstrap["definitions"]["project"]["dependencies"]["modelReferenceScope"]
        == test_case.expected_model_reference_scope
    )
    assert (
        tuple(bootstrap["definitions"]["project"]["dependencies"]["allowedCrossPipelineReferences"])
        == test_case.expected_allowed_cross_pipeline_references
    )


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="definitions expose the protected pipeline operator gate",
            expected_value={
                "warning": "Interrupts protected order events.",
                "confirmation": "DEPLOY_ORDER_EVENTS",
            },
        )
    ],
    ids=lambda case: case.description,
)
def test_given_protected_pipeline_when_reading_definitions_then_serializes_protection(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    (tmp_path / "pipelines" / "order_events" / "pipeline.toml").write_text(
        """
mode = "direct"

[protection]
warning = "Interrupts protected order events."
confirmation = "DEPLOY_ORDER_EVENTS"
""".strip(),
        encoding="utf-8",
    )
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    payload: dict = client.get("/api/definitions").json()

    pipeline: dict = named_payload_item(payload["pipelines"], "order_events")
    assert pipeline["protection"] == test_case.expected_value
    assert pipeline["directory"] == "pipelines/order_events"


@pytest.mark.parametrize(
    "test_case",
    [
        ConnectionSettingsPayloadTestCase(
            description="nested connection settings serialize as plain JSON data",
            expected_settings={"max_threads": "16"},
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_nested_connection_settings_when_reading_definitions_then_payload_serializes(
    test_case: ConnectionSettingsPayloadTestCase,
    tmp_path: Path,
) -> None:
    write_connection_settings_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)

    response: NativeResponse = client.get("/api/definitions")

    assert response.status_code == 200
    assert response.json()["project"]["connection"]["settings"] == test_case.expected_settings


@pytest.mark.parametrize(
    "test_case",
    [
        DevRefactorTestCase(
            description="matching compile version returns not modified", expected_value=304
        )
    ],
    ids=lambda case: case.description,
)
def test_given_matching_definitions_etag_when_reading_then_payload_is_not_retransmitted(
    test_case: DevRefactorTestCase,
    tmp_path: Path,
) -> None:
    write_dev_server_project(project_dir=tmp_path)
    client: NativeTestClient = build_test_client(project_dir=tmp_path)
    first: NativeResponse = client.get("/api/definitions")

    cached: NativeResponse = client.get(
        "/api/definitions", headers={"If-None-Match": first.headers["etag"]}
    )

    assert first.status_code == 200
    assert cached.status_code == test_case.expected_value
    assert cached.content == b""
