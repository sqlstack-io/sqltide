import { DEFAULT_PRESUMED_FAILED_AFTER_SECONDS, RUN_EVENT_WINDOW_SIZE } from '../constants.ts';
import { assembleRuns, parseTimestamp } from '../_helpers/run-assembly.ts';
import { eventStreams, terminalRuns } from '../_api/run-history.ts';
import { captureWarehouseTimestamp } from '../_api/warehouse-probes.ts';
import type { Streams } from '../_helpers/run-assembly.ts';
import { statementProgress } from '../_api/statement-progress.ts';
import type { Query, RunEvent, RunEventsPayload, RunPayload } from '../types.ts';

const RUN_EVENT_PAGE_SIZE: number = 500;

/** `GET /api/runs/{id}/events?after=N`: one page of durable events and the run's derived state. */
export async function readRunEvents(
	query: Query,
	database: string,
	invocationId: string,
	after: number = 0,
	presumedFailedAfterSeconds: number = DEFAULT_PRESUMED_FAILED_AFTER_SECONDS
): Promise<RunEventsPayload> {
	const warehouseTimestamp: string = await captureWarehouseTimestamp(query);
	const terminal: Map<string, RunPayload> = await terminalRuns(query, database, { invocationId });
	const statusStreams: Streams = await eventStreams(query, database, {
		invocationId,
		recentLimit: RUN_EVENT_WINDOW_SIZE
	});
	const [run]: RunPayload[] = assembleRuns(
		terminal,
		statusStreams,
		parseTimestamp(warehouseTimestamp),
		presumedFailedAfterSeconds
	);
	const page: Streams = await eventStreams(query, database, { invocationId, after, rowLimit: RUN_EVENT_PAGE_SIZE });
	const events: RunEvent[] = page.get(invocationId) ?? [];
	return {
		found: run !== undefined || events.length > 0,
		events,
		hasMore: events.length === RUN_EVENT_PAGE_SIZE,
		status: run?.status ?? null,
		lastSignalAt: run?.lastSignalAt ?? null,
		lastSignalAgeSeconds: run?.lastSignalAgeSeconds ?? null,
		statementProgress: await statementProgress(query, statusStreams.get(invocationId) ?? [], warehouseTimestamp)
	};
}
