/** Run payloads and events shaped from rows of the run tables. */
import type { JsonObject, RunEvent, RunPayload } from '../types.ts';
import type { Streams } from './run-assembly.ts';
import { get, getOr, parsedObject, pyStr, toInt } from './values.ts';

export type InvocationRow = {
	invocation_id: string;
	project_identity: string;
	command: string;
	mode: string | null;
	outcome: string;
	exit_code: unknown;
	started_at: string;
	completed_at: string;
	duration_ms: unknown;
	selected_node_count: unknown;
	error_message: string | null;
	summary_json: string;
	tool_version: string;
};

export type EventRow = {
	invocation_id: string;
	sequence: unknown;
	emitted_at: string;
	event_kind: string;
	step_id: string | null;
	phase: string | null;
	payload_json: string;
};

/** The outcome counts of a completed scheduled audit cycle. */
export function terminalAuditSummary(command: string, mode: string, summaryJson: unknown): JsonObject | null {
	if (command !== 'audit' || mode !== 'scheduled') return null;
	const summary: JsonObject = parsedObject(summaryJson);
	if (!Object.hasOwn(summary, 'scheduled_count') && !Object.hasOwn(summary, 'error_count')) return null;
	if (get(summary, 'error_count') !== null) {
		const errors: number = toInt(summary.error_count);
		return { passed: 0, warning: 0, failed: 0, error: errors, total: errors };
	}
	const total: number = toInt(getOr(summary, 'scheduled_count', 0));
	const warning: number = toInt(getOr(summary, 'warning_failure_count', 0));
	const failed: number = toInt(getOr(summary, 'error_failure_count', 0));
	const error: number = toInt(getOr(summary, 'execution_error_count', 0));
	return { passed: Math.max(total - warning - failed - error, 0), warning, failed, error, total };
}

export function terminalRun(row: InvocationRow): RunPayload {
	return {
		invocationId: row.invocation_id,
		projectIdentity: pyStr(row.project_identity),
		command: pyStr(row.command),
		displayCommand: null,
		mode: pyStr(row.mode),
		status: pyStr(row.outcome),
		outcome: pyStr(row.outcome),
		exitCode: toInt(row.exit_code),
		startedAt: pyStr(row.started_at),
		completedAt: pyStr(row.completed_at),
		lastSignalAt: pyStr(row.completed_at),
		lastSignalAgeSeconds: 0,
		durationMs: toInt(row.duration_ms),
		selectedNodeCount: toInt(row.selected_node_count),
		errorMessage: row.error_message,
		summary: parsedObject(row.summary_json),
		auditSummary: terminalAuditSummary(pyStr(row.command), pyStr(row.mode), row.summary_json),
		toolVersion: pyStr(row.tool_version),
		lastActivity: null,
		completedOperationCount: null,
		totalStatements: null,
		currentStep: null
	};
}

/** Event rows grouped by invocation, each event merged with its JSON payload. */
export function streamsFromRows(rows: EventRow[]): Streams {
	const streams: Streams = new Map();
	for (const row of rows) {
		const invocationId: string = pyStr(row.invocation_id);
		const stream: RunEvent[] = streams.get(invocationId) ?? [];
		streams.set(invocationId, stream);
		stream.push({
			sequence: toInt(row.sequence),
			emittedAt: pyStr(row.emitted_at),
			event: pyStr(row.event_kind),
			stepId: row.step_id,
			phase: row.phase,
			...parsedObject(row.payload_json)
		} as RunEvent);
	}
	return streams;
}
