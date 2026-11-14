import {
	DEFAULT_PRESUMED_FAILED_AFTER_SECONDS,
	INVOCATIONS_TABLE,
	RUN_EVENT_WINDOW_SIZE,
	RUN_EVENTS_TABLE
} from '../constants.ts';
import { assembleRuns, parseTimestamp } from '../_helpers/run-assembly.ts';
import { eventStreams, runStartedStreams, terminalRuns } from '../_api/run-history.ts';
import { captureWarehouseTimestamp, tableExists } from '../_api/warehouse-probes.ts';
import type { Streams } from '../_helpers/run-assembly.ts';
import type { Query, RunPayload } from '../types.ts';

const DEFAULT_RUNS_LIMIT: number = 100;
const SCHEDULED_RUNS_LIMIT: number = 25;

/** `GET /api/runs`: terminal runs and unterminated event streams with derived states. */
export async function readRuns(
	query: Query,
	database: string,
	presumedFailedAfterSeconds: number = DEFAULT_PRESUMED_FAILED_AFTER_SECONDS
): Promise<RunPayload[]> {
	const warehouseNow: number = parseTimestamp(await captureWarehouseTimestamp(query));
	const invocationsPresent: boolean = await tableExists(query, database, INVOCATIONS_TABLE);
	const eventsPresent: boolean = await tableExists(query, database, RUN_EVENTS_TABLE);
	const terminal: Map<string, RunPayload> = new Map();
	if (invocationsPresent) {
		for (const [limit, scheduled] of [
			[DEFAULT_RUNS_LIMIT, false],
			[SCHEDULED_RUNS_LIMIT, true]
		] as const) {
			for (const [id, run] of await terminalRuns(query, database, { limit, scheduled }, true)) terminal.set(id, run);
		}
	}
	const streams: Streams = await runStartedStreams(query, database, [...terminal.keys()], eventsPresent);
	const active: Streams = await eventStreams(
		query,
		database,
		{ recentLimit: RUN_EVENT_WINDOW_SIZE, activeOnly: true, excludeTerminalInvocations: invocationsPresent },
		eventsPresent
	);
	for (const [id, events] of active) streams.set(id, events);
	return assembleRuns(terminal, streams, warehouseNow, presumedFailedAfterSeconds);
}
