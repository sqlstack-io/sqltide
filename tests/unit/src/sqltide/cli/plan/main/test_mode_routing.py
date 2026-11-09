import json
from pathlib import Path

import pytest

from sqltide.adapter.models import AdapterDirectFingerprintSnapshot
from sqltide.cli.plan.constants import DIRECT_MODE_LABEL
from sqltide.cli.workflow_artifacts.main._publish_plan_workflow import publish_plan_workflow
from sqltide.compiler.planner.models import DirectWarehouseSnapshot
from sqltide.executor.workflow.models import BuildWorkflow
from sqltide.executor.workflow.types import WorkflowMode
from tests.unit.src.sqltide.cli.plan.main._test_types import (
    CliChangedPlanTestCase,
    CliChangedSourceDriftPlanTestCase,
    CliDirectPlanFlagRejectionTestCase,
    CliPlanDeploymentIdRejectionTestCase,
    CliPlanModeRoutingTestCase,
    CliPlanPublicationFailureTestCase,
)
from tests.unit.src.sqltide.cli.plan.main.helpers import (
    build_drifted_source_plan_inputs,
    fail_second_workflow_artifact_replace,
    run_scope_project_plan,
)
from tests.unit.src.sqltide.compiler.planner.helpers import (
    analyze_direct_scope_project,
    build_direct_fingerprint_snapshot,
    write_direct_scope_project,
)

_TOPIC_DRIFT: tuple[tuple[str, str], ...] = (("kafka_topic_list", "source.orders.previous"),)


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanModeRoutingTestCase(
            description="an omitted virtual_environments setting plans in direct mode",
            virtual_environments=None,
            expected_mode=DIRECT_MODE_LABEL,
            expected_title="Direct plan",
        ),
        CliPlanModeRoutingTestCase(
            description="a disabled virtual_environments setting plans in direct mode",
            virtual_environments=False,
            expected_mode=DIRECT_MODE_LABEL,
            expected_title="Direct plan",
        ),
        CliPlanModeRoutingTestCase(
            description="an enabled virtual_environments setting preserves deployment planning",
            virtual_environments=True,
            expected_mode="virtual environments",
            expected_title="Plan Ready",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_effective_project_mode_when_planning_then_that_mode_is_used(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: CliPlanModeRoutingTestCase
) -> None:
    write_direct_scope_project(
        project_root=tmp_path, virtual_environments=test_case.virtual_environments
    )
    virtual_environments: bool = test_case.virtual_environments is True
    deployment_id: str | None = {
        False: None,
        True: "20260802T120000Z_planmode",
    }[virtual_environments]

    text_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=False,
        virtual_environments=virtual_environments,
        deployment_id=deployment_id,
    )
    text_output: str = capsys.readouterr().out
    artifact_path: Path = tmp_path / test_case.expected_artifact_path
    text_artifact: bytes = artifact_path.read_bytes()
    json_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        virtual_environments=virtual_environments,
        deployment_id=deployment_id,
    )
    json_output: str = capsys.readouterr().out
    payload: dict[str, object] = json.loads(json_output)

    assert (text_exit_code, json_exit_code) == (0, 0)
    assert test_case.expected_title in text_output
    assert payload["mode"] == test_case.expected_mode
    assert payload["adapter"] == "clickhouse"
    assert json.loads(text_artifact) == payload
    assert artifact_path.read_bytes() == json_output.encode("utf-8")


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanModeRoutingTestCase(
            description="direct mode reports the complete closure and its single replay root",
            virtual_environments=None,
            expected_mode=DIRECT_MODE_LABEL,
            expected_title="Direct Plan",
            expected_execution_scope=("alpha", "beta", "gamma", "delta"),
            expected_replay_root_models=("alpha",),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_direct_mode_when_planning_then_full_closure_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], test_case: CliPlanModeRoutingTestCase
) -> None:
    write_direct_scope_project(
        project_root=tmp_path, virtual_environments=test_case.virtual_environments
    )

    exit_code: int = run_scope_project_plan(project_root=tmp_path, json_output=True)

    payload: dict[str, object] = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert tuple(key["name"] for key in payload["execution_scope"]) == (
        test_case.expected_execution_scope
    )
    assert payload["database"] == "analytics"
    assert (
        tuple(root["model_key"]["name"] for root in payload["replay_roots"])
        == test_case.expected_replay_root_models
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedPlanTestCase(
            description="changed roots expand to their downstream closure",
            expected_execution_scope=("beta", "gamma", "delta"),
            expected_reasons=(
                "changed",
                "downstream_of_selected",
                "downstream_of_selected",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_changed_model_when_planning_then_changed_root_and_downstream_are_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedPlanTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)
    fingerprints: AdapterDirectFingerprintSnapshot = build_direct_fingerprint_snapshot(
        analysis=analyze_direct_scope_project(project_root=tmp_path),
        changed_model_names=("beta",),
    )

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
    )

    payload: dict[str, object] = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["selection_mode"] == "changed"
    assert tuple(item["model_key"]["name"] for item in payload["entries"]) == (
        test_case.expected_execution_scope
    )
    assert tuple(item["reason"] for item in payload["entries"]) == test_case.expected_reasons


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedPlanTestCase(
            description="no changed models produce an empty direct plan",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_no_changed_models_when_planning_then_plan_is_a_no_op(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedPlanTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)
    fingerprints: AdapterDirectFingerprintSnapshot = build_direct_fingerprint_snapshot(
        analysis=analyze_direct_scope_project(project_root=tmp_path),
    )

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
    )

    payload: dict[str, object] = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["selection_mode"] == "changed"
    assert tuple(payload["execution_scope"]) == test_case.expected_execution_scope
    assert payload["entries"] == []


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="a declared setting absent from the deployed source selects its reader",
            declared_settings=(("kafka_max_block_size", "500"),),
            catalog_setting_overrides=(),
            omitted_catalog_settings=("kafka_max_block_size",),
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_drifted_settings=("kafka_max_block_size",),
            expected_reasons=(*(("source_drift",) * 4), "downstream_of_selected"),
            expected_selected_pipelines=("pl__orders",),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="a changed topic selects its reader like an explicit pipeline selection",
            declared_settings=(),
            catalog_setting_overrides=(("kafka_topic_list", "source.orders.previous"),),
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_drifted_settings=("kafka_topic_list",),
            expected_reasons=(*(("source_drift",) * 4), "downstream_of_selected"),
            expected_selected_pipelines=("pl__orders",),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_drifted_managed_source_when_planning_changed_then_owner_plan_matches_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        extra_reader_pipelines=test_case.extra_reader_pipelines,
        changed_model_names=test_case.changed_model_names,
        protected_pipelines=test_case.protected_pipelines,
    )

    changed_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    changed_payload: dict = json.loads(capsys.readouterr().out)
    explicit_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        selectors=(test_case.owner_selector,),
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    explicit_payload: dict = json.loads(capsys.readouterr().out)

    assert (changed_exit_code, explicit_exit_code) == (0, 0)
    assert changed_payload["selection_mode"] == "changed"
    assert tuple(key["name"] for key in changed_payload["execution_scope"]) == (
        test_case.expected_execution_scope
    )
    assert tuple(
        recreation["relation_name"] for recreation in changed_payload["source_recreations"]
    ) == (test_case.expected_recreated_relation_names)
    assert [
        recreation["drifted_settings"] for recreation in changed_payload["source_recreations"]
    ] == [list(test_case.expected_drifted_settings)]
    assert {
        field: changed_payload[field]
        for field in ("execution_scope", "teardown", "creation", "replay_roots")
    } == {
        field: explicit_payload[field]
        for field in ("execution_scope", "teardown", "creation", "replay_roots")
    }
    assert changed_payload["source_recreations"] == explicit_payload["source_recreations"]
    assert tuple(entry["reason"] for entry in changed_payload["entries"]) == (
        test_case.expected_reasons
    )
    assert tuple(
        selection["pipeline_name"] for selection in changed_payload["source_drift_selections"]
    ) == (test_case.expected_selected_pipelines)
    assert explicit_payload["source_drift_selections"] == []


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="a deployed source matching its declaration selects nothing",
            declared_settings=(("kafka_max_block_size", "500"),),
            catalog_setting_overrides=(),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="a masked deployed setting selects nothing",
            declared_settings=(),
            catalog_setting_overrides=(("kafka_broker_list", "[HIDDEN]"),),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_undrifted_managed_source_when_planning_changed_then_nothing_is_selected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        extra_reader_pipelines=test_case.extra_reader_pipelines,
        changed_model_names=test_case.changed_model_names,
        protected_pipelines=test_case.protected_pipelines,
    )

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )

    payload: dict = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert tuple(payload["execution_scope"]) == test_case.expected_execution_scope
    assert payload["source_recreations"] == []


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="settings drift under another consumer group blocks like a selection",
            declared_settings=(),
            catalog_setting_overrides=(
                ("kafka_topic_list", "source.orders.previous"),
                ("kafka_group_name", "sqltide_orders_previous"),
            ),
            expected_error_fragment=(
                "pipeline 'pl__orders' was selected because source 'orders' settings drifted "
                "from the project, and kafka__orders consumes as consumer group "
                "'sqltide_orders_previous'"
            ),
            expected_text_fragment=(
                "has drifted: kafka__orders consumes as consumer group 'sqltide_orders_previous'"
            ),
            unexpected_error_fragment="was selected because",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_blocking_source_drift_when_planning_changed_then_it_fails_like_a_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        extra_reader_pipelines=test_case.extra_reader_pipelines,
        changed_model_names=test_case.changed_model_names,
        protected_pipelines=test_case.protected_pipelines,
    )

    changed_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    changed_error: str = capsys.readouterr().err
    explicit_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        selectors=(test_case.owner_selector,),
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    explicit_error: str = capsys.readouterr().err

    assert (changed_exit_code, explicit_exit_code) == (1, 1)
    assert test_case.expected_error_fragment in changed_error
    assert test_case.expected_text_fragment in explicit_error
    assert test_case.unexpected_error_fragment not in explicit_error


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="one of two direct readers is rebuilt, chosen by sorted pipeline name",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            expected_execution_scope=("order_audit_model",),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=("source_drift",),
            expected_selected_pipelines=("pl__order_audit",),
            expected_text_fragment=(
                "orders settings drifted (kafka_topic_list); --changed rebuilds "
                "pipeline:pl__order_audit to recreate it; pl__orders keep reading the preserved "
                "landing table"
            ),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="a reader already rebuilt for changed SQL adds no other pipeline",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            changed_model_names=("alpha",),
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=("changed", *(("downstream_of_selected",) * 4)),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="a reader rebuilt downstream of changed SQL adds no other pipeline",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            changed_model_names=("beta",),
            expected_execution_scope=("beta", "gamma", "delta"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=("changed", "downstream_of_selected", "downstream_of_selected"),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="an unprotected reader is preferred over a protected earlier name",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            protected_pipelines=("pl__order_audit",),
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=(*(("source_drift",) * 4), "downstream_of_selected"),
            expected_selected_pipelines=("pl__orders",),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="an unprotected earlier name is kept over a protected later reader",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            protected_pipelines=("pl__orders",),
            expected_execution_scope=("order_audit_model",),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=("source_drift",),
            expected_selected_pipelines=("pl__order_audit",),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="among protected readers the first name is chosen",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "direct"),),
            protected_pipelines=("pl__order_audit", "pl__orders"),
            expected_execution_scope=("order_audit_model",),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=("source_drift",),
            expected_selected_pipelines=("pl__order_audit",),
        ),
        CliChangedSourceDriftPlanTestCase(
            description="a virtual reader is skipped in favour of the direct one",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            extra_reader_pipelines=(("pl__order_audit", "virtual"),),
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
            expected_recreated_relation_names=("kafka__orders",),
            expected_reasons=(*(("source_drift",) * 4), "downstream_of_selected"),
            expected_selected_pipelines=("pl__orders",),
            expected_text_fragment="--changed rebuilds pipeline:pl__orders to recreate it",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_drifted_source_readers_when_planning_changed_then_one_owner_is_rebuilt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        extra_reader_pipelines=test_case.extra_reader_pipelines,
        changed_model_names=test_case.changed_model_names,
        protected_pipelines=test_case.protected_pipelines,
    )

    json_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    payload: dict = json.loads(capsys.readouterr().out)
    text_exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=False,
        changed=True,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )
    text: str = capsys.readouterr().out

    assert (json_exit_code, text_exit_code) == (0, 0)
    assert tuple(key["name"] for key in payload["execution_scope"]) == (
        test_case.expected_execution_scope
    )
    assert tuple(entry["reason"] for entry in payload["entries"]) == test_case.expected_reasons
    assert tuple(
        selection["pipeline_name"] for selection in payload["source_drift_selections"]
    ) == (test_case.expected_selected_pipelines)
    assert tuple(recreation["relation_name"] for recreation in payload["source_recreations"]) == (
        test_case.expected_recreated_relation_names
    )
    assert test_case.expected_text_fragment in text


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="a start time is refused for a pipeline selected only for source drift",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            start_time="2026-08-01T00:00:00Z",
            expected_error_fragment=(
                "--changed selects pipeline:pl__orders because the settings of source 'orders' "
                "drifted from the project, and --start-time would rebuild their models from "
                "that time only, discarding earlier history. Run `sqt build --select "
                "pipeline:pl__orders` without --start-time first, then rerun with --changed "
                "--start-time."
            ),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_drift_selected_pipeline_when_planning_changed_from_start_time_then_it_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
    )

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        start_time=test_case.start_time,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedSourceDriftPlanTestCase(
            description="a start time stays available when changed SQL already rebuilds the reader",
            declared_settings=(),
            catalog_setting_overrides=_TOPIC_DRIFT,
            changed_model_names=("alpha",),
            start_time="2026-08-01T00:00:00Z",
            expected_execution_scope=("alpha", "beta", "gamma", "delta", "order_summary"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_sql_changed_reader_when_planning_changed_from_start_time_then_it_is_planned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedSourceDriftPlanTestCase,
) -> None:
    snapshot: DirectWarehouseSnapshot
    fingerprints: AdapterDirectFingerprintSnapshot
    snapshot, fingerprints = build_drifted_source_plan_inputs(
        project_root=tmp_path,
        declared_settings=test_case.declared_settings,
        catalog_setting_overrides=test_case.catalog_setting_overrides,
        omitted_catalog_settings=test_case.omitted_catalog_settings,
        changed_model_names=test_case.changed_model_names,
    )

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        start_time=test_case.start_time,
        direct_fingerprints=fingerprints,
        warehouse_snapshot=snapshot,
    )

    payload: dict = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert tuple(key["name"] for key in payload["execution_scope"]) == (
        test_case.expected_execution_scope
    )
    assert payload["source_drift_selections"] == []


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedPlanTestCase(
            description="virtual-only changed selection is rejected",
            expected_error_fragment="--changed is only supported for direct models",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_virtual_models_when_selecting_changed_then_planning_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedPlanTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path, virtual_environments=True)

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        virtual_environments=True,
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedPlanTestCase(
            description="changed and explicit selectors are mutually exclusive",
            selectors=("alpha",),
            expected_error_fragment="--changed cannot be combined with --select",
        ),
        CliChangedPlanTestCase(
            description="missing-upstream expansion requires a selection mode",
            changed=False,
            include_missing_upstream=True,
            expected_error_fragment=("--include-missing-upstream requires --changed or --select"),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_incompatible_changed_selection_flags_when_planning_then_command_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedPlanTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        selectors=test_case.selectors,
        changed=test_case.changed,
        include_missing_upstream=test_case.include_missing_upstream,
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        CliChangedPlanTestCase(
            description="unavailable fingerprints fail closed",
            expected_error_fragment="Cannot select changed models: metadata denied",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_unavailable_fingerprints_when_selecting_changed_then_planning_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliChangedPlanTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=True,
        changed=True,
        direct_fingerprints=AdapterDirectFingerprintSnapshot(
            status="unavailable",
            baselines=(),
            warning="metadata denied",
        ),
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanDeploymentIdRejectionTestCase(
            description="deployment identity is rejected in direct mode",
            deployment_id="20260802T120000Z_directinvalid",
            expected_error_fragment="--deployment-id requires virtual environments",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_deployment_id_in_direct_mode_when_planning_then_command_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliPlanDeploymentIdRejectionTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=False,
        deployment_id=test_case.deployment_id,
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err
    assert not (tmp_path / "target/run/plan").exists()


@pytest.mark.parametrize(
    "test_case",
    [
        CliDirectPlanFlagRejectionTestCase(
            description="--full-refresh is rejected in direct mode",
            selectors=("alpha",),
            full_refresh=True,
            start_time=None,
            expected_error_fragment="--full-refresh is a virtual-environment replay control",
            expected_preserved_artifact=b'{"previous":"plan"}\n',
        ),
        CliDirectPlanFlagRejectionTestCase(
            description="--start-time without selection is rejected before inspection",
            selectors=(),
            full_refresh=False,
            start_time="2026-01-01",
            expected_error_fragment="--start-time requires --changed or at least one --select",
            expected_preserved_artifact=b'{"previous":"plan"}\n',
        ),
        CliDirectPlanFlagRejectionTestCase(
            description="--start-time and --full-refresh remain mutually exclusive",
            selectors=("alpha",),
            full_refresh=True,
            start_time="2026-01-01",
            expected_error_fragment="--full-refresh cannot be combined with --start-time",
            expected_preserved_artifact=b'{"previous":"plan"}\n',
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_full_refresh_in_direct_mode_when_planning_then_command_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_case: CliDirectPlanFlagRejectionTestCase,
) -> None:
    write_direct_scope_project(project_root=tmp_path)
    artifact_path: Path = tmp_path / "target/run/plan/plan.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(test_case.expected_preserved_artifact)

    exit_code: int = run_scope_project_plan(
        project_root=tmp_path,
        json_output=False,
        selectors=test_case.selectors,
        full_refresh=test_case.full_refresh,
        start_time=test_case.start_time,
    )

    assert exit_code == 1
    assert test_case.expected_error_fragment in capsys.readouterr().err
    assert artifact_path.read_bytes() == test_case.expected_preserved_artifact


@pytest.mark.parametrize(
    "test_case",
    [
        CliPlanPublicationFailureTestCase(
            description="a failed atomic replacement preserves the previous complete plan",
            previous_artifact=b'{"previous":"plan"}\n',
            replacement_artifact='{"replacement":"plan"}\n',
            expected_error_fragment="publication failed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_plan_when_atomic_publication_fails_then_preserves_previous_artifact(
    test_case: CliPlanPublicationFailureTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path: Path = tmp_path / "run/plan/plan.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(test_case.previous_artifact)
    fail_second_workflow_artifact_replace(
        monkeypatch=monkeypatch,
        error_message=test_case.expected_error_fragment,
    )

    with pytest.raises(OSError, match=test_case.expected_error_fragment):
        publish_plan_workflow(
            target_dir=tmp_path,
            workflow=BuildWorkflow(
                mode=WorkflowMode.DIRECT,
                plan_json=test_case.replacement_artifact,
                statements=(),
            ),
        )

    assert artifact_path.read_bytes() == test_case.previous_artifact
    assert tuple(artifact_path.parent.iterdir()) == (artifact_path,)
