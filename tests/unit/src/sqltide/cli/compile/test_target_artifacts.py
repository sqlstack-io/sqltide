import json
import os
from pathlib import Path
from unittest.mock import DEFAULT, Mock

import pytest

from sqltide._native import run_cli
from tests.unit.src.sqltide.cli.compile._test_types import (
    AdoptedCompileTargetTestCase,
    CompileArtifactIdentityTestCase,
    CompileCheckArtifactsTestCase,
    CompileDiagnosticOutputTestCase,
    CompileGenerationFailureTestCase,
    DerivedSourceManifestTestCase,
    EmptyCompileTargetTestCase,
    ExactCompileTargetTestCase,
    PublicationRollbackTestCase,
    RemovedStaticInputsTestCase,
    SourceRetentionManifestTestCase,
    SourceSecretRedactionTestCase,
    StaticReplacementTestCase,
    ViewCompileTargetTestCase,
)
from tests.unit.src.sqltide.cli.compile.helpers import (
    compile_project,
    compile_project_with_render_failure,
    copy_basic_project,
    copy_orders_demo,
    static_target_snapshot,
    target_file_paths,
    target_snapshot,
    write_adopted_source,
    write_artifact_leaf_model,
    write_cross_pipeline_model_reference,
    write_empty_project,
    write_invalid_model,
    write_invalid_model_header,
    write_invalid_reference_model,
    write_secret_source,
    write_typed_source_retention,
    write_view_project,
)


@pytest.mark.parametrize(
    "test_case",
    [
        ExactCompileTargetTestCase(
            description="writes the exact managed static target without runtime evidence",
            expected_relative_files=(
                "compiled/models/pl__orders/orders_enriched.sql",
                "compiled/resources/models/pl__orders/orders_enriched.mv.sql",
                "compiled/resources/models/pl__orders/orders_enriched.table.sql",
                "compiled/resources/sources/orders/kafka__orders.sql",
                "compiled/resources/sources/orders/mv__orders.sql",
                "compiled/resources/sources/orders/raw__orders.sql",
                "manifest.json",
                "sqltide_dag.json",
            ),
            expected_forbidden_path="run",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_managed_project_when_compiling_then_writes_exact_static_target_tree(
    test_case: ExactCompileTargetTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)

    assert exit_code == 0
    assert target_file_paths(target_dir=target_dir) == test_case.expected_relative_files
    assert not (target_dir / test_case.expected_forbidden_path).exists()


@pytest.mark.parametrize(
    "test_case",
    [
        ViewCompileTargetTestCase(
            description="ordinary view writes query, resource, and manifest artifacts",
            expected_relative_files=(
                "compiled/models/pl__consumer/customer_orders.sql",
                "compiled/resources/models/pl__consumer/customer_orders.view.sql",
                "manifest.json",
                "sqltide_dag.json",
            ),
            expected_resource_kind="view",
            expected_relation_name="customer_orders",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_view_project_when_compiling_then_writes_ordinary_view_artifacts(
    test_case: ViewCompileTargetTestCase, tmp_path: Path
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    write_view_project(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    model_entry: dict[str, object] = manifest["models"]["customer_orders"]
    resource_entry: dict[str, object] = model_entry["resources"][0]

    assert exit_code == 0
    assert target_file_paths(target_dir=target_dir) == test_case.expected_relative_files
    assert resource_entry["kind"] == test_case.expected_resource_kind
    assert model_entry["relation_name"] == test_case.expected_relation_name
    assert model_entry["source"] is None
    assert model_entry["spec"] is None


@pytest.mark.parametrize(
    "test_case",
    [
        AdoptedCompileTargetTestCase(
            description="records adopted identity without claiming source resource candidates",
            expected_relation_name="existing_orders",
            expected_source_resource_count=0,
            expected_forbidden_workflow_path="compiled/workflows",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_adopted_source_when_compiling_then_omits_managed_source_candidates(
    test_case: AdoptedCompileTargetTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    write_adopted_source(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    source_entry: dict[str, object] = manifest["sources"]["orders"]

    assert exit_code == 0
    assert source_entry["relation_name"] == test_case.expected_relation_name
    assert len(source_entry["resources"]) == test_case.expected_source_resource_count
    assert not (target_dir / test_case.expected_forbidden_workflow_path).exists()


@pytest.mark.parametrize(
    "test_case",
    [
        SourceRetentionManifestTestCase(
            description="manifest records effective typed Kafka retention",
            expected_ttl=(
                "least(ifNull(_replay_timestamp, _replay_landed_at), "
                "_replay_landed_at) + INTERVAL 12 HOUR"
            ),
            expected_origin="project",
            expected_duration_seconds=43_200,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_typed_kafka_retention_when_compiling_then_manifest_records_policy_and_ttl(
    test_case: SourceRetentionManifestTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    write_typed_source_retention(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    source_entry: dict[str, object] = manifest["sources"]["orders"]
    retention: dict[str, object] = source_entry["retention"]

    assert exit_code == 0
    assert source_entry["ttl"] == test_case.expected_ttl
    assert retention["origin"] == test_case.expected_origin
    assert retention["duration_seconds"] == test_case.expected_duration_seconds


@pytest.mark.parametrize(
    "test_case",
    [
        DerivedSourceManifestTestCase(
            description="manifest exposes a derived Kafka source name origin",
            expected_name="orders",
            expected_origin="derived",
            expected_macro_name="kafka_source_name",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_macro_named_kafka_source_when_compiling_then_manifest_exposes_derived_origin(
    test_case: DerivedSourceManifestTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    (project_dir / "sqltide_project.toml").write_text(
        """
name = "basic_project"
default_target = "test"

[defaults]
pipeline_mode = "virtual"

[defaults.sources.kafka]
naming_macro = "kafka_source_name"

[targets.test]
database = "analytics"
""".strip(),
        encoding="utf-8",
    )
    (project_dir / "sources" / "orders.yml").write_text(
        """sources:
  - kind: kafka
    broker_list: kafka:9092
    topic: source.orders.created
    replay_boundary:
      mode: offsets
""",
        encoding="utf-8",
    )
    macros_dir: Path = project_dir / "macros"
    macros_dir.mkdir()
    (macros_dir / "source_names.py").write_text(
        "def kafka_source_name(topic: str) -> str:\n    return topic.split('.')[1]\n",
        encoding="utf-8",
    )

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    source_entry: dict[str, object] = manifest["sources"][test_case.expected_name]
    name_origin: dict[str, object] = source_entry["name_origin"]

    assert exit_code == 0
    assert source_entry["name"] == test_case.expected_name
    assert name_origin["kind"] == test_case.expected_origin
    assert name_origin["macro"] == test_case.expected_macro_name
    assert len(name_origin["macro_fingerprint"]) == 64


@pytest.mark.parametrize(
    "test_case",
    [
        StaticReplacementTestCase(
            description="replaces all static owners deterministically while preserving runtime",
            stale_relative_paths=(
                "compiled/models/pl__orders/removed_model.sql",
                "compiled/tests/removed/removed_test.sql",
                "compiled/audits/removed_audit.sql",
            ),
            runtime_relative_path="run/plan/plan.json",
            runtime_contents=b'{"exact":"runtime bytes"}\n',
            legacy_relative_path="orders/run/workflow/old_candidate.sql",
            expected_exit_code=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_stale_static_and_runtime_files_when_recompiling_then_replaces_only_static_owners(
    test_case: StaticReplacementTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    _ = compile_project(project_dir=project_dir, target_dir=target_dir)
    baseline_snapshot: tuple[tuple[str, bytes], ...] = static_target_snapshot(target_dir=target_dir)
    stale_path: str
    for stale_path in test_case.stale_relative_paths:
        (target_dir / stale_path).parent.mkdir(parents=True, exist_ok=True)
        (target_dir / stale_path).write_text("stale", encoding="utf-8")
    runtime_path: Path = target_dir / test_case.runtime_relative_path
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(test_case.runtime_contents)
    legacy_path: Path = target_dir / test_case.legacy_relative_path
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("old compile candidate", encoding="utf-8")
    (target_dir / "manifest.json").write_text(
        json.dumps({"pipelines": {"compiled": {}, "orders": {}, "run": {}}}) + "\n",
        encoding="utf-8",
    )

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    rerun_static_snapshot: tuple[tuple[str, bytes], ...] = static_target_snapshot(
        target_dir=target_dir
    )

    assert exit_code == test_case.expected_exit_code
    assert rerun_static_snapshot == baseline_snapshot
    assert tuple((target_dir / path).exists() for path in test_case.stale_relative_paths) == (
        False,
        False,
        False,
    )
    assert runtime_path.read_bytes() == test_case.runtime_contents
    assert not legacy_path.exists()


@pytest.mark.parametrize(
    "test_case",
    [
        CompileGenerationFailureTestCase(
            description="preserves the complete prior static target when generation fails",
            expected_error_fragment="artifact render failure",
            expected_snapshot_preserved=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_static_target_when_generation_fails_then_preserves_previous_snapshot(
    test_case: CompileGenerationFailureTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    _ = compile_project(project_dir=project_dir, target_dir=target_dir)
    baseline_snapshot: tuple[tuple[str, bytes], ...] = target_snapshot(target_dir=target_dir)

    with pytest.raises(RuntimeError, match=test_case.expected_error_fragment):
        compile_project_with_render_failure(project_dir=project_dir, target_dir=target_dir)

    assert (target_snapshot(target_dir=target_dir) == baseline_snapshot) is (
        test_case.expected_snapshot_preserved
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileArtifactIdentityTestCase(
            description="manifest and DAG agree on logical and realized resource identities",
            expected_manifest_sources=("orders",),
            expected_manifest_models=("orders_enriched",),
            expected_dag_node_ids=("source:orders", "model:orders_enriched"),
            expected_edge=("source:orders", "model:orders_enriched", "driving_input"),
            expected_model_reference_scope="project",
            project_config_suffix="",
        ),
        CompileArtifactIdentityTestCase(
            description="manifest records explicit pipeline model reference scope",
            expected_manifest_sources=("orders",),
            expected_manifest_models=("orders_enriched",),
            expected_dag_node_ids=("source:orders", "model:orders_enriched"),
            expected_edge=("source:orders", "model:orders_enriched", "driving_input"),
            expected_model_reference_scope="pipeline",
            project_config_suffix=('\n[dependencies]\nmodel_reference_scope = "pipeline"\n'),
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_one_analysis_when_writing_manifest_and_dag_then_artifacts_agree(
    test_case: CompileArtifactIdentityTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    project_config_path: Path = project_dir / "sqltide_project.toml"
    project_config_path.write_text(
        project_config_path.read_text(encoding="utf-8") + test_case.project_config_suffix,
        encoding="utf-8",
    )
    _ = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    dag: dict[str, object] = json.loads((target_dir / "sqltide_dag.json").read_text())

    assert tuple(manifest["sources"]) == test_case.expected_manifest_sources
    assert tuple(manifest["models"]) == test_case.expected_manifest_models
    assert tuple(node["id"] for node in dag["nodes"]) == test_case.expected_dag_node_ids
    assert tuple((edge["from_id"], edge["to_id"], edge["edge_type"]) for edge in dag["edges"]) == (
        test_case.expected_edge,
    )
    assert manifest["dependencies"]["model_reference_scope"] == (
        test_case.expected_model_reference_scope
    )
    assert tuple(manifest["dependencies"]["allowed_cross_pipeline_references"]) == (
        test_case.expected_allowed_cross_pipeline_references
    )
    assert not any(path.startswith("compiled/workflows/") for path in manifest["artifacts"])


@pytest.mark.parametrize(
    "test_case",
    [
        CompileCheckArtifactsTestCase(
            description="writes assembled tests and mirrored audits from the compiled project",
            expected_test_path=(
                "compiled/tests/order_events/order events derive stable region labels.sql"
            ),
            expected_audit_path=(
                "compiled/audits/order_events/no_future_events__orders_no_future_events.sql"
            ),
            expected_test_target="order_events",
            expected_audit_model="order_events",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_tests_and_audits_when_compiling_then_writes_static_check_artifacts(
    test_case: CompileCheckArtifactsTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_orders_demo(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())
    test_entry: dict[str, object] = manifest["tests"]["order events derive stable region labels"]
    audit_entry: dict[str, object] = manifest["audits"]["orders_no_future_events"]

    assert exit_code == 0
    assert test_entry["path"] == test_case.expected_test_path
    assert audit_entry["path"] == test_case.expected_audit_path
    assert tuple(test_entry["targets"]) == (test_case.expected_test_target,)
    assert tuple(audit_entry["referenced_models"]) == (test_case.expected_audit_model,)


@pytest.mark.parametrize(
    "test_case",
    [
        CompileDiagnosticOutputTestCase(
            description="renders compile errors with structured file line and phase context",
            expected_exit_code=1,
            expected_error_fragments=(
                "error [SQT-COMPILE-001]",
                "phase: compilation",
                "resource: orders_enriched",
                "orders_enriched.sql:6:8",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_model_when_compiling_then_cli_renders_structured_source_diagnostic(
    test_case: CompileDiagnosticOutputTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copy_basic_project(project_dir=project_dir)
    write_invalid_model(project_dir=project_dir)

    exit_code: int = run_cli(("sqt", "compile", "--project-dir", str(project_dir)))
    stderr: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    assert tuple(fragment in stderr for fragment in test_case.expected_error_fragments) == (
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileDiagnosticOutputTestCase(
            description="renders discovery errors with the authored model location",
            expected_exit_code=1,
            expected_error_fragments=(
                "error [SQT-DISCOVERY-001]",
                "phase: discovery",
                "resource: orders_enriched",
                "orders_enriched.sql:1:1",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_invalid_model_header_when_compiling_then_cli_renders_discovery_diagnostic(
    test_case: CompileDiagnosticOutputTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copy_basic_project(project_dir=project_dir)
    write_invalid_model_header(project_dir=project_dir)

    exit_code: int = run_cli(("sqt", "compile", "--project-dir", str(project_dir)))
    stderr: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    assert tuple(fragment in stderr for fragment in test_case.expected_error_fragments) == (
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileDiagnosticOutputTestCase(
            description="renders malformed reference errors at the authored query span",
            expected_exit_code=1,
            expected_error_fragments=(
                "error [SQT-DISCOVERY-001]",
                "phase: discovery",
                "orders_enriched.sql:6:",
                "unclosed quoted text",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_malformed_reference_when_compiling_then_cli_renders_authored_source_diagnostic(
    test_case: CompileDiagnosticOutputTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copy_basic_project(project_dir=project_dir)
    write_invalid_reference_model(project_dir=project_dir)

    exit_code: int = run_cli(("sqt", "compile", "--project-dir", str(project_dir)))
    stderr: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    assert tuple(fragment in stderr for fragment in test_case.expected_error_fragments) == (
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        CompileDiagnosticOutputTestCase(
            description="renders pipeline-scope graph errors at the authored reference",
            expected_exit_code=1,
            expected_error_fragments=(
                "error [SQT-GRAPH-001]",
                "phase: graph",
                "beta.sql:6:",
                "references model 'orders_enriched'",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_cross_pipeline_model_ref_when_compiling_then_cli_renders_graph_diagnostic(
    test_case: CompileDiagnosticOutputTestCase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_dir: Path = tmp_path / "project"
    copy_basic_project(project_dir=project_dir)
    write_cross_pipeline_model_reference(project_dir=project_dir)

    exit_code: int = run_cli(("sqt", "compile", "--project-dir", str(project_dir)))
    stderr: str = capsys.readouterr().err

    assert exit_code == test_case.expected_exit_code
    assert tuple(fragment in stderr for fragment in test_case.expected_error_fragments) == (
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "test_case",
    [
        EmptyCompileTargetTestCase(
            description="publishes a complete empty static target",
            expected_relative_files=("manifest.json", "sqltide_dag.json"),
            expected_pipeline_count=0,
            expected_model_count=0,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_empty_project_when_compiling_then_publishes_empty_compiled_owner(
    test_case: EmptyCompileTargetTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    write_empty_project(project_dir=project_dir)

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest: dict[str, object] = json.loads((target_dir / "manifest.json").read_text())

    assert exit_code == 0
    assert target_file_paths(target_dir=target_dir) == test_case.expected_relative_files
    assert (target_dir / "compiled").is_dir()
    assert len(manifest["pipelines"]) == test_case.expected_pipeline_count
    assert len(manifest["models"]) == test_case.expected_model_count


@pytest.mark.parametrize(
    "test_case",
    [
        SourceSecretRedactionTestCase(
            description="redacts broker userinfo and sensitive Kafka settings from every artifact",
            broker_secret="broker-secret-sentinel",
            setting_secret="setting-secret-sentinel",
            expected_placeholder="***",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_secret_bearing_source_when_compiling_then_redacts_all_static_artifacts(
    test_case: SourceSecretRedactionTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    write_secret_source(project_dir=project_dir)

    exit_code: int = compile_project(
        project_dir=project_dir,
        target_dir=target_dir,
        environment={
            "BROKER_LIST": f"kafka://user:{test_case.broker_secret}@broker:9092",
            "KAFKA_SASL_PASSWORD": test_case.setting_secret,
        },
    )
    artifact_bytes: bytes = b"\n".join(path.read_bytes() for path in target_dir.glob("**/*.*"))

    assert exit_code == 0
    assert test_case.broker_secret.encode() not in artifact_bytes
    assert test_case.setting_secret.encode() not in artifact_bytes
    assert test_case.expected_placeholder.encode() in artifact_bytes


@pytest.mark.parametrize(
    "test_case",
    [
        PublicationRollbackTestCase(
            description="restores every prior static owner when publication fails midway",
            failing_replace_call=4,
            expected_error_fragment="injected rename failure",
            expected_snapshot_preserved=True,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_existing_target_when_atomic_publication_fails_then_rolls_back_all_owners(
    test_case: PublicationRollbackTestCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_basic_project(project_dir=project_dir)
    _ = compile_project(project_dir=project_dir, target_dir=target_dir)
    baseline_snapshot: tuple[tuple[str, bytes], ...] = target_snapshot(target_dir=target_dir)
    side_effects: tuple[object, ...] = (
        *(DEFAULT for _index in range(test_case.failing_replace_call - 1)),
        OSError(test_case.expected_error_fragment),
        DEFAULT,
        DEFAULT,
    )
    replace_mock: Mock = Mock(wraps=os.replace, side_effect=side_effects)
    monkeypatch.setattr(
        "os.replace",
        replace_mock,
    )

    with pytest.raises(OSError, match=test_case.expected_error_fragment):
        compile_project(project_dir=project_dir, target_dir=target_dir)

    assert (target_snapshot(target_dir=target_dir) == baseline_snapshot) is (
        test_case.expected_snapshot_preserved
    )


@pytest.mark.parametrize(
    "test_case",
    [
        RemovedStaticInputsTestCase(
            description="removes model test and audit artifacts after authored inputs disappear",
            removed_relative_inputs=(
                "pipelines/pl__order_events/artifact_leaf.sql",
                "tests/order_events/test_order_events.sql",
                "audits/order_events/no_future_events.sql",
            ),
            expected_removed_artifacts=(
                "compiled/models/pl__order_events/artifact_leaf.sql",
                "compiled/tests/order_events/order events derive stable region labels.sql",
                "compiled/audits/order_events/no_future_events__orders_no_future_events.sql",
            ),
            expected_removed_manifest_names=(
                "artifact_leaf",
                "order events derive stable region labels",
                "orders_no_future_events",
            ),
            expected_removed_dag_node_ids=(
                "model:artifact_leaf",
                "test:order events derive stable region labels",
                "audit:orders_no_future_events",
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_removed_authored_inputs_when_recompiling_then_removes_all_static_references(
    test_case: RemovedStaticInputsTestCase,
    tmp_path: Path,
) -> None:
    project_dir: Path = tmp_path / "project"
    target_dir: Path = project_dir / "target"
    copy_orders_demo(project_dir=project_dir)
    write_artifact_leaf_model(project_dir=project_dir)
    _ = compile_project(project_dir=project_dir, target_dir=target_dir)
    relative_input: str
    for relative_input in test_case.removed_relative_inputs:
        (project_dir / relative_input).unlink()

    exit_code: int = compile_project(project_dir=project_dir, target_dir=target_dir)
    manifest_text: str = (target_dir / "manifest.json").read_text()
    dag_text: str = (target_dir / "sqltide_dag.json").read_text()

    assert exit_code == 0
    assert tuple(
        (target_dir / artifact_path).exists()
        for artifact_path in test_case.expected_removed_artifacts
    ) == (False, False, False)
    assert tuple(
        resource_name in manifest_text
        for resource_name in test_case.expected_removed_manifest_names
    ) == (False, False, False)
    assert tuple(node_id in dag_text for node_id in test_case.expected_removed_dag_node_ids) == (
        False,
        False,
        False,
    )
