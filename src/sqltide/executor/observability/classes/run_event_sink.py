"""Stream run events to stdout JSONL and the durable run-events table."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from hashlib import sha256
from typing import TextIO
from uuid import uuid4

import sqltide._native as _native
from sqltide.adapter.classes.adapter_connection import AdapterConnection
from sqltide.adapter.constants import (
    REDACTED_SECRET_PLACEHOLDER,
)
from sqltide.adapter.exceptions import AdapterWarehouseError
from sqltide.adapter.models import (
    AdapterQueryResult,
    AdapterReplayOffsetProgressRequest,
    AdapterRunEventRecord,
    AdapterRunStatementRecord,
)
from sqltide.cli.build.constants import SQLTIDE_TOOL_VERSION
from sqltide.executor.observability._helpers.payload import bounded_json
from sqltide.executor.observability._helpers.workflow import assemble_observation_workflow
from sqltide.executor.observability.constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    RUN_DISPLAY_COMMAND_ENV_VAR,
)
from sqltide.executor.observability.models import RunStartupTimings
from sqltide.executor.workflow.main._execute_observation_workflow import (
    execute_observation_workflow,
)
from sqltide.executor.workflow.models import WarehouseStatement

_RUN_STARTED_KIND: str = "run_started"
_RUN_COMPLETED_KIND: str = "run_completed"
_STATEMENT_STARTED_KIND: str = "statement_started"
_STATEMENT_COMPLETED_KIND: str = "statement_completed"
_RUN_HEARTBEAT_KIND: str = "run_heartbeat"
_AUDIT_STARTED_KIND: str = "audit_started"
_AUDIT_COMPLETED_KIND: str = "audit_completed"
_KAFKA_ENGINE_PATTERN: re.Pattern[str] = re.compile(r"\bENGINE\s*=\s*Kafka\b", re.IGNORECASE)
_QUOTED_SETTING_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)"
    r"'(?P<value>(?:\\.|''|[^'])*)'"
)
_SENSITIVE_SETTING_FRAGMENTS: tuple[str, ...] = (
    "credential",
    "password",
    "private_key",
    "sasl",
    "secret",
    "token",
)
_KAFKA_BROKER_LIST_SETTING: str = "kafka_broker_list"
_BROKER_USERINFO_SEPARATOR: str = "@"


def _replay_offset_progress_payload(*, statement: WarehouseStatement) -> dict[str, object] | None:
    request: AdapterReplayOffsetProgressRequest | None = statement.replay_offset_progress
    if request is None:
        return None
    return {
        "database": request.database,
        "relation": request.relation,
        "partitionColumn": request.partition_column,
        "offsetColumn": request.offset_column,
        "ranges": [
            {
                "partition": replay_range.partition,
                "lowerOffset": replay_range.lower_offset,
                "upperOffset": replay_range.upper_offset,
            }
            for replay_range in request.ranges
        ],
    }


class RunEventSink:
    """Best-effort run narration: JSONL for pipes plus durable rows for readers."""

    def __init__(
        self,
        *,
        connection: AdapterConnection,
        database: str,
        invocation_id: str,
        project_identity: str | None = None,
        jsonl_stream: TextIO | None = None,
        strict_persistence: bool = False,
    ) -> None:
        self._connection: AdapterConnection = connection
        self._database: str = database
        self._invocation_id: str = invocation_id
        self._project_identity: str | None = project_identity
        self._jsonl_stream: TextIO | None = jsonl_stream
        self._strict_persistence: bool = strict_persistence
        self._sequence: int = 0
        self._migrated: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._heartbeat_stop: threading.Event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._persistence_warning_emitted: bool = False
        self._workflow_revision: int = 0
        self._operation_evidence: dict[str, object] | None = None

    def set_operation_evidence(self, evidence: dict[str, object] | None) -> None:
        """Set complete operation evidence for the next run boundary event."""

        self._operation_evidence = evidence

    def run_started(
        self,
        *,
        command: str,
        mode: str,
        total_statements: int,
        selected_node_count: int,
        display_command: str | None = None,
        selectors: tuple[str, ...] = (),
        start_time: str | None = None,
        executed_logical_ids: tuple[str, ...] = (),
        context_logical_ids: tuple[str, ...] = (),
        startup_timings: RunStartupTimings | None = None,
    ) -> None:
        """The workflow is assembled and about to execute."""

        self._emit(
            event_kind=_RUN_STARTED_KIND,
            step_id=None,
            phase=None,
            payload={
                "command": command,
                "displayCommand": (
                    display_command
                    if display_command is not None
                    else os.environ.get(RUN_DISPLAY_COMMAND_ENV_VAR, command)
                ),
                "mode": mode,
                "toolVersion": SQLTIDE_TOOL_VERSION,
                "totalStatements": total_statements,
                "selectedNodeCount": selected_node_count,
                "database": self._database,
                "projectIdentity": self._project_identity,
                "selectors": list(selectors),
                "startTime": start_time,
                "executedLogicalIds": list(executed_logical_ids),
                "contextLogicalIds": list(context_logical_ids),
                "startupTimings": (
                    None
                    if startup_timings is None
                    else {
                        "compileMs": startup_timings.compile_ms,
                        "observabilityMs": startup_timings.observability_ms,
                        "planningMs": startup_timings.planning_ms,
                        "totalMs": startup_timings.total_ms,
                    }
                ),
                "operationEvidence": self._operation_evidence,
            },
            complete_payload=True,
        )
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def run_completed(
        self,
        *,
        outcome: str,
        exit_code: int,
        error_message: str | None,
    ) -> None:
        """The run reached a terminal state; closes the event stream."""

        self.stop()
        self._emit(
            event_kind=_RUN_COMPLETED_KIND,
            step_id=None,
            phase=None,
            payload={
                "outcome": outcome,
                "exitCode": exit_code,
                "errorMessage": error_message,
                "operationEvidence": self._operation_evidence,
            },
            complete_payload=self._operation_evidence is not None,
        )

    def stop(self) -> None:
        """Stop the heartbeat without requiring a terminal event write."""

        self._heartbeat_stop.set()
        heartbeat_thread: threading.Thread | None = self._heartbeat_thread
        if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
            heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS)

    def statement_started(self, statement: WarehouseStatement) -> str:
        """WorkflowEventEmitter: one statement is about to execute."""

        query_id: str = str(uuid4())
        self._emit(
            event_kind=_STATEMENT_STARTED_KIND,
            step_id=statement.step_id,
            phase=str(statement.phase),
            payload={
                "statementSequence": statement.sequence,
                "intent": str(statement.intent),
                "queryId": query_id,
                "displayName": statement.display_name,
                "replayOffsetProgress": _replay_offset_progress_payload(statement=statement),
            },
        )
        return query_id

    def workflow_prepared(
        self, *, statements: tuple[WarehouseStatement, ...], workflow_sha256: str
    ) -> None:
        """Persist and verify the statement set, skipping adapters that render none."""

        if not statements:
            return
        next_revision: int = self._workflow_revision + 1
        redacted_sql: tuple[str, ...] = tuple(
            _redacted_statement_sql(statement.sql) for statement in statements
        )
        persisted_workflow_sha256: str = (
            workflow_sha256
            if all(
                persisted_sql == statement.sql
                for statement, persisted_sql in zip(statements, redacted_sql, strict=True)
            )
            else sha256("\n".join(redacted_sql).encode()).hexdigest()
        )
        records: tuple[AdapterRunStatementRecord, ...] = tuple(
            AdapterRunStatementRecord(
                invocation_id=self._invocation_id,
                statement_sequence=statement.sequence,
                step_id=statement.step_id,
                phase=str(statement.phase),
                intent=str(statement.intent),
                sql=persisted_sql,
                sql_sha256=sha256(persisted_sql.encode()).hexdigest(),
                workflow_sha256=persisted_workflow_sha256,
                workflow_revision=next_revision,
            )
            for statement, persisted_sql in zip(statements, redacted_sql, strict=True)
        )
        rendered: tuple[str, ...] = self._connection.render_run_statements(
            database=self._database,
            statements=records,
            include_migration=not self._migrated,
        )
        if not rendered:
            if self._strict_persistence:
                raise AdapterWarehouseError(
                    "Adapter did not provide durable run-statement persistence"
                )
            return
        self._workflow_revision = next_revision
        observation_statements: tuple[WarehouseStatement, ...] = assemble_observation_workflow(
            rendered
        )
        _ = execute_observation_workflow(
            statements=observation_statements,
            connection=self._connection,
        )
        self._migrated = True
        self._verify_workflow_statements(records=records)

    def _verify_workflow_statements(
        self, *, records: tuple[AdapterRunStatementRecord, ...]
    ) -> None:
        result: AdapterQueryResult = self._connection.query(
            _native.render_clickhouse_run_statement_verification_query(
                self._database, self._invocation_id
            )
        )
        observed: tuple[tuple[int, str, str], ...] = tuple(
            (int(str(row[0])), str(row[1]), str(row[2])) for row in result.rows
        )
        expected: tuple[tuple[int, str, str], ...] = tuple(
            (record.statement_sequence, record.sql_sha256, record.workflow_sha256)
            for record in records
        )
        if observed != expected:
            raise AdapterWarehouseError(
                "Run statement persistence verification failed; warehouse execution was not "
                f"started (expected {len(expected)} rows, observed {len(observed)} rows)"
            )

    def audit_started(self, *, name: str) -> None:
        """One scheduled audit is about to execute."""

        self._emit(
            event_kind=_AUDIT_STARTED_KIND,
            step_id=name,
            phase="audit",
            payload={"name": name},
        )

    def audit_completed(
        self, *, name: str, status: str, failure_count: int, error_message: str | None
    ) -> None:
        """One scheduled audit reached a terminal result."""

        self._emit(
            event_kind=_AUDIT_COMPLETED_KIND,
            step_id=name,
            phase="audit",
            payload={
                "name": name,
                "status": status,
                "failureCount": failure_count,
                "errorMessage": error_message,
            },
        )

    def statement_completed(
        self,
        *,
        statement: WarehouseStatement,
        error_message: str | None,
        written_rows: int | None,
        elapsed_ms: int,
    ) -> None:
        """WorkflowEventEmitter: one statement finished, successfully or not."""

        self._emit(
            event_kind=_STATEMENT_COMPLETED_KIND,
            step_id=statement.step_id,
            phase=str(statement.phase),
            payload={
                "statementSequence": statement.sequence,
                "intent": str(statement.intent),
                "elapsedMs": elapsed_ms,
                "writtenRows": written_rows,
                "errorMessage": error_message,
                "displayName": statement.display_name,
            },
        )

    def _emit(
        self,
        *,
        event_kind: str,
        step_id: str | None,
        phase: str | None,
        payload: dict[str, object],
        complete_payload: bool = False,
    ) -> None:
        with self._lock:
            self._sequence += 1
            record: AdapterRunEventRecord = AdapterRunEventRecord(
                invocation_id=self._invocation_id,
                sequence=self._sequence,
                event_kind=event_kind,
                step_id=step_id,
                phase=phase,
                payload_json=(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
                    if complete_payload
                    else bounded_json(payload)
                ),
            )
            self._write_jsonl(record=record, payload=payload)
            self._persist(record)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            self._emit(
                event_kind=_RUN_HEARTBEAT_KIND,
                step_id=None,
                phase=None,
                payload={},
            )

    def _write_jsonl(self, *, record: AdapterRunEventRecord, payload: dict[str, object]) -> None:
        if self._jsonl_stream is None:
            return
        line: dict[str, object] = {
            "event": record.event_kind,
            "invocationId": record.invocation_id,
            "sequence": record.sequence,
            "stepId": record.step_id,
            "phase": record.phase,
            **payload,
        }
        try:
            print(json.dumps(line, default=str), file=self._jsonl_stream, flush=True)
        except Exception:
            return

    def _persist(self, record: AdapterRunEventRecord) -> None:
        try:
            rendered: tuple[str, ...] = self._connection.render_run_events(
                database=self._database,
                events=(record,),
                include_migration=not self._migrated,
            )
            statements: tuple[WarehouseStatement, ...] = assemble_observation_workflow(rendered)
            _ = execute_observation_workflow(statements=statements, connection=self._connection)
            self._migrated = True
            self._persistence_warning_emitted = False
        except Exception as error:
            if self._strict_persistence:
                raise AdapterWarehouseError(
                    f"Required run-event persistence failed: {error}"
                ) from error
            if not self._persistence_warning_emitted:
                try:
                    print(
                        f"run observation failed; continuing execution and retrying: {error}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
                self._persistence_warning_emitted = True
            return


def _redacted_statement_sql(sql: str) -> str:
    """Remove credential-bearing Kafka settings from durable SQL text."""

    if _KAFKA_ENGINE_PATTERN.search(sql) is None:
        return sql

    def replace_setting(match: re.Match[str]) -> str:
        key: str = match.group("key")
        value: str = match.group("value")
        broker_list_with_userinfo: bool = (
            key.lower() == _KAFKA_BROKER_LIST_SETTING and _BROKER_USERINFO_SEPARATOR in value
        )
        sensitive: bool = (
            any(fragment in key.lower() for fragment in _SENSITIVE_SETTING_FRAGMENTS)
            or broker_list_with_userinfo
        )
        if not sensitive:
            return match.group(0)
        return f"{match.group('prefix')}'{REDACTED_SECRET_PLACEHOLDER}'"

    return _QUOTED_SETTING_PATTERN.sub(replace_setting, sql)
