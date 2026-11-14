/** Run payloads assembled from terminal invocations and event streams, with derived status. */
import type { JsonObject, RunEvent, RunPayload } from '../types.ts';
import { compareText, get, getOr, pyStr, timestampMicros, toInt } from './values.ts';

/** Event streams by invocation, in row order. */
export type Streams = Map<string, RunEvent[]>;

/** Seconds without a signal after which a running run is reported unresponsive. */
const RUN_UNRESPONSIVE_AFTER_SECONDS: number = 45;

const TERMINAL_STATUSES: readonly string[] = ['succeeded', 'failed', 'cancelled'];

/** Microseconds of a warehouse timestamp, read as UTC. */
export function parseTimestamp(value: string): number {
	const micros: number | null = timestampMicros(value);
	if (micros === null) throw new Error(`Invalid isoformat string: '${value}'`);
	return micros;
}

/** `max(int((later - earlier).total_seconds()), 0)`. */
function wholeSeconds(later: number, earlier: number): number {
	return Math.max(Math.trunc((later - earlier) / 1e6), 0);
}

export function ageSeconds(timestamp: string, warehouseNow: number): number {
	return wholeSeconds(warehouseNow, parseTimestamp(timestamp));
}

function deriveStatus(
	terminalOutcome: string | null,
	completedOutcome: string | null,
	lastSignalAt: number,
	warehouseNow: number,
	presumedFailedAfterSeconds: number
): string {
	for (const outcome of [terminalOutcome, completedOutcome]) {
		if (outcome !== null && TERMINAL_STATUSES.includes(outcome)) return outcome;
	}
	const signalAge: number = Math.max((warehouseNow - lastSignalAt) / 1e6, 0);
	if (signalAge < RUN_UNRESPONSIVE_AFTER_SECONDS) return 'running';
	if (signalAge < presumedFailedAfterSeconds) return 'unresponsive';
	return 'presumed_failed';
}

function firstOf(events: RunEvent[], kind: string): RunEvent | undefined {
	return events.find((event) => event.event === kind);
}

function lastOf(events: RunEvent[], kind: string): RunEvent | undefined {
	return events.findLast((event) => event.event === kind);
}

/** Take one completion for `key`; `false` when none is left to pair with a start. */
function consume(counts: Map<string, number>, key: string): boolean {
	const count: number = counts.get(key) ?? 0;
	if (count === 0) return false;
	if (count === 1) counts.delete(key);
	else counts.set(key, count - 1);
	return true;
}

function bump(counts: Map<string, number>, key: string): void {
	counts.set(key, (counts.get(key) ?? 0) + 1);
}

/** The step of the latest statement or audit that started without a matching completion. */
function activeStep(events: RunEvent[]): string | null {
	const statements: Map<string, number> = new Map();
	const audits: Map<string, number> = new Map();
	for (const event of [...events].reverse()) {
		const kind: string = pyStr(event.event);
		const sequence: unknown = get(event, 'statementSequence');
		const stepId: unknown = get(event, 'stepId');
		if (kind === 'statement_completed' && sequence !== null) bump(statements, String(toInt(sequence)));
		else if (kind === 'audit_completed' && stepId !== null) bump(audits, pyStr(stepId));
		else if (kind === 'statement_started') {
			if (sequence !== null && consume(statements, String(toInt(sequence)))) continue;
			return stepId === null ? null : pyStr(stepId);
		} else if (kind === 'audit_started') {
			if (stepId === null) return null;
			if (consume(audits, pyStr(stepId))) continue;
			return pyStr(stepId);
		}
	}
	return null;
}

/** The latest started statement without a matching completion. */
export function activeStatement(events: RunEvent[]): RunEvent | null {
	const statements: Map<string, number> = new Map();
	for (const event of [...events].reverse()) {
		const kind: string = pyStr(event.event);
		const sequence: unknown = get(event, 'statementSequence');
		if (kind === 'statement_completed' && sequence !== null) bump(statements, String(toInt(sequence)));
		else if (kind === 'statement_started' && sequence !== null && !consume(statements, String(toInt(sequence)))) {
			return event;
		}
	}
	return null;
}

function eventAuditSummary(events: RunEvent[]): JsonObject | null {
	const counts: JsonObject & Record<'passed' | 'warning' | 'failed' | 'error', number> = {
		passed: 0,
		warning: 0,
		failed: 0,
		error: 0
	};
	for (const event of events) {
		if (event.event !== 'audit_completed') continue;
		const status: string = pyStr(getOr(event, 'status', ''));
		if (status === 'passed' || status === 'warning' || status === 'failed' || status === 'error') counts[status] += 1;
	}
	const total: number = counts.passed + counts.warning + counts.failed + counts.error;
	return total === 0 ? null : { ...counts, total };
}

function runProgress(events: RunEvent[]): JsonObject {
	const started: RunEvent | undefined = firstOf(events, 'run_started');
	const rawTotal: unknown = started === undefined ? null : get(started, 'totalStatements');
	return {
		completedOperationCount: events.filter(
			(event) => event.event === 'statement_completed' || event.event === 'audit_completed'
		).length,
		totalStatements: rawTotal === null ? null : toInt(rawTotal),
		currentStep: activeStep(events),
		auditSummary: eventAuditSummary(events)
	};
}

function lastActivity(events: RunEvent[]): string | null {
	const event: RunEvent | undefined = events.findLast((item) => get(item, 'stepId') !== null);
	return event === undefined ? null : pyStr(event.stepId);
}

/** The run payload of an event stream with no durable terminal fact. */
function activeRun(
	invocationId: string,
	events: RunEvent[],
	failure: RunEvent | undefined,
	cancellationCompleted: boolean,
	warehouseNow: number,
	presumedFailedAfterSeconds: number
): RunPayload {
	const started: RunEvent = events[0];
	const completed: RunEvent | undefined = lastOf(events, 'run_completed');
	const lastSignalAt: string = pyStr(events[events.length - 1].emittedAt);
	let status: string = deriveStatus(
		null,
		completed === undefined ? null : pyStr(get(completed, 'outcome')),
		parseTimestamp(lastSignalAt),
		warehouseNow,
		presumedFailedAfterSeconds
	);
	if (failure !== undefined) status = 'cancellation_failed';
	else if (cancellationCompleted) status = 'cancelled';
	const startedAt: string = pyStr(started.emittedAt);
	const command: unknown = getOr(started, 'command', 'build');
	const end: number = completed === undefined ? warehouseNow : parseTimestamp(pyStr(completed.emittedAt));
	const run: RunPayload = {
		invocationId,
		command: pyStr(command),
		displayCommand: pyStr(getOr(started, 'displayCommand', command)),
		mode: pyStr(getOr(started, 'mode', 'unknown')),
		status,
		outcome: status,
		exitCode: completed === undefined ? null : get(completed, 'exitCode'),
		startedAt,
		completedAt: completed === undefined ? null : completed.emittedAt,
		lastSignalAt,
		lastSignalAgeSeconds: ageSeconds(lastSignalAt, warehouseNow),
		durationMs: wholeSeconds(end, parseTimestamp(startedAt)) * 1000,
		selectedNodeCount: toInt(getOr(started, 'selectedNodeCount', 0)),
		executedLogicalIds: get(started, 'executedLogicalIds'),
		contextLogicalIds: get(started, 'contextLogicalIds'),
		errorMessage: completed === undefined ? null : get(completed, 'errorMessage'),
		toolVersion: pyStr(getOr(started, 'toolVersion', '')),
		projectIdentity: get(started, 'projectIdentity'),
		lastActivity: lastActivity(events),
		...runProgress(events)
	};
	if (failure !== undefined) run.errorMessage = get(failure, 'errorMessage');
	return run;
}

/** Merge terminal invocations with their event streams into run payloads, newest start first. */
export function assembleRuns(
	terminal: Map<string, RunPayload>,
	streams: Streams,
	warehouseNow: number,
	presumedFailedAfterSeconds: number
): RunPayload[] {
	const copies: Map<string, RunPayload> = new Map([...terminal].map(([id, run]) => [id, { ...run }]));
	const runs: RunPayload[] = [...copies.values()];
	for (const [invocationId, events] of streams) {
		const failure: RunEvent | undefined = lastOf(events, 'cancellation_failed');
		const cancellationCompleted: boolean = firstOf(events, 'cancellation_completed') !== undefined;
		const run: RunPayload | undefined = copies.get(invocationId);
		if (run === undefined) {
			runs.push(activeRun(invocationId, events, failure, cancellationCompleted, warehouseNow, presumedFailedAfterSeconds));
			continue;
		}
		const started: RunEvent | undefined = firstOf(events, 'run_started');
		if (started !== undefined && Boolean(get(started, 'displayCommand'))) run.displayCommand = pyStr(started.displayCommand);
		if (events.some((event) => event.event !== 'run_started')) {
			run.lastSignalAt = events[events.length - 1].emittedAt;
			const progress: JsonObject = runProgress(events);
			if (get(run, 'auditSummary') !== null) delete progress.auditSummary;
			Object.assign(run, progress);
		}
		if (failure !== undefined) {
			run.status = 'cancellation_failed';
			run.outcome = 'cancellation_failed';
			run.errorMessage = get(failure, 'errorMessage');
		} else if (cancellationCompleted) {
			run.status = 'cancelled';
			run.outcome = 'cancelled';
		}
	}
	// A stable descending sort keeps equal start times in their original order.
	return runs
		.map((run, index) => ({ run, index }))
		.sort((left, right) => compareText(pyStr(right.run.startedAt), pyStr(left.run.startedAt)) || left.index - right.index)
		.map(({ run }) => run);
}
