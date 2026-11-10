from pathlib import Path

import pytest

from sqltide._native import publish_build_workflow
from sqltide.executor.workflow.main.execute_build_workflow import execute_build_workflow
from sqltide.executor.workflow.models import (
    BuildWorkflow,
    PublishedBuildWorkflow,
    WorkflowExecutionResult,
)
from tests.unit.src.sqltide.cli.helpers import RecordingAdapterConnection
from tests.unit.src.sqltide.executor.workflow._test_types import (
    WorkflowExecutionTestCase,
    WorkflowPersistenceFailureTestCase,
    WorkflowPublicationTestCase,
)
from tests.unit.src.sqltide.executor.workflow.helpers import (
    FailingPreparationEmitter,
    build_test_workflow,
)


@pytest.mark.parametrize(
    "test_case",
    [
        WorkflowPublicationTestCase(
            description="publishes exact ordered workflow bytes before returning capability",
            expected_plan_json='{"mode":"direct"}\n',
            expected_workflow_sql="SELECT 1;\nINSERT INTO events VALUES (1);",
            expected_step_filenames=("0001_check_ready.sql", "0002_insert_event.sql"),
            expected_workflow_sha256=(
                "d46ce2917103b8f34fe706ebba3fbac49e5dfe955df90afdb63ee3bead83d2b9"
            ),
        )
    ],
    ids=lambda case: case.description,
)
def test_given_valid_workflow_when_publishing_then_every_artifact_uses_exact_statement_bytes(
    tmp_path: Path,
    test_case: WorkflowPublicationTestCase,
) -> None:
    workflow: BuildWorkflow = build_test_workflow(plan_json=test_case.expected_plan_json)

    published: PublishedBuildWorkflow = publish_build_workflow(
        target_dir=tmp_path,
        workflow=workflow,
    )

    step_root: Path = published.artifact_root / "steps"
    assert published.workflow is workflow
    assert (published.artifact_root / "plan.json").read_text() == test_case.expected_plan_json
    assert (published.artifact_root / "workflow.sql").read_text() == (
        test_case.expected_workflow_sql
    )
    assert len(tuple(step_root.iterdir())) == len(test_case.expected_step_filenames)
    assert tuple(
        (step_root / filename).read_text() for filename in test_case.expected_step_filenames
    ) == tuple(statement.sql for statement in workflow.statements)
    assert published.workflow_sha256 == test_case.expected_workflow_sha256


@pytest.mark.parametrize(
    "test_case",
    [
        WorkflowExecutionTestCase(
            description="dispatches exact statement bytes in workflow tuple order",
            expected_statements=("SELECT 1;", "INSERT INTO events VALUES (1);"),
            expected_query_result_count=1,
            expected_mutation_result_count=1,
        )
    ],
    ids=lambda case: case.description,
)
def test_given_published_workflow_when_executing_then_gateway_receives_exact_mutation_bytes(
    tmp_path: Path,
    test_case: WorkflowExecutionTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection()
    published: PublishedBuildWorkflow = publish_build_workflow(
        target_dir=tmp_path,
        workflow=build_test_workflow(plan_json='{"mode":"direct"}\n'),
    )

    result: WorkflowExecutionResult = execute_build_workflow(
        published_workflow=published,
        connection=connection,
    )

    assert tuple(connection.statements) == test_case.expected_statements
    assert result.statement_results[0].query_result is not None
    assert result.statement_results[0].mutation_result is None
    assert result.statement_results[1].query_result is None
    assert result.statement_results[1].mutation_result is not None
    assert test_case.expected_query_result_count == 1
    assert test_case.expected_mutation_result_count == 1


@pytest.mark.parametrize(
    "test_case",
    [
        WorkflowPersistenceFailureTestCase(
            description="persistence failure prevents any workflow statement dispatch",
            expected_error_fragment="statement persistence failed",
        )
    ],
    ids=lambda case: case.description,
)
def test_given_statement_persistence_failure_when_executing_then_no_workflow_sql_runs(
    tmp_path: Path,
    test_case: WorkflowPersistenceFailureTestCase,
) -> None:
    connection: RecordingAdapterConnection = RecordingAdapterConnection()
    published: PublishedBuildWorkflow = publish_build_workflow(
        target_dir=tmp_path,
        workflow=build_test_workflow(plan_json='{"mode":"direct"}\n'),
    )

    with pytest.raises(RuntimeError, match=test_case.expected_error_fragment):
        execute_build_workflow(
            published_workflow=published,
            connection=connection,
            emitter=FailingPreparationEmitter(),
        )

    assert connection.statements == []
