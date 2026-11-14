/** Live warehouse progress of a run's active statement from `system.processes`. */
import type { JsonObject, Query, RunEvent } from '../types.ts';
import { compareText, get, pyStr, sqlLiteral, toInt } from '../_helpers/values.ts';
import { activeStatement } from '../_helpers/run-assembly.ts';

type ProcessRow = {
	elapsed: unknown;
	read_rows: unknown;
	read_bytes: unknown;
	total_rows_approx: unknown;
	memory_usage: unknown;
	settings: { [name: string]: unknown } | null;
};

async function loadProcess(query: Query, queryId: string): Promise<ProcessRow | null> {
	try {
		const rows: ProcessRow[] = await query<ProcessRow>(
			'SELECT elapsed, read_rows, read_bytes, total_rows_approx, memory_usage, Settings AS settings ' +
				`FROM system.processes WHERE query_id = '${sqlLiteral(queryId)}' LIMIT 1`
		);
		return rows[0] ?? null;
	} catch {
		// Progress is optional: a warehouse failure reads as no progress.
		return null;
	}
}

/**
 * The active statement's live progress, or `null` when no statement is running. Replay offset
 * progress needs the server's frontier observer and is not reported here.
 */
export async function statementProgress(
	query: Query,
	events: RunEvent[],
	observedAt: string
): Promise<JsonObject | null> {
	const active: RunEvent | null = activeStatement(events);
	if (active === null || get(active, 'queryId') === null) return null;
	const queryId: string = pyStr(active.queryId);
	const progress: ProcessRow | null = await loadProcess(query, queryId);
	const payload: JsonObject = {
		found: progress !== null,
		queryId,
		statementSequence: toInt(active.statementSequence),
		stepId: get(active, 'stepId'),
		phase: get(active, 'phase'),
		observedAt
	};
	if (progress === null) return payload;
	const elapsed: number = Number(progress.elapsed);
	const readRows: number = toInt(progress.read_rows);
	const readBytes: number = toInt(progress.read_bytes);
	const settings: [string, string][] = Object.entries(progress.settings ?? {})
		.map(([name, value]): [string, string] => [name, pyStr(value)])
		.sort(([left], [right]) => compareText(left, right));
	return {
		...payload,
		elapsedSeconds: elapsed,
		readRows,
		readBytes,
		totalRowsApprox: toInt(progress.total_rows_approx),
		memoryUsageBytes: toInt(progress.memory_usage),
		readRowsPerSecond: elapsed <= 0 ? 0 : readRows / elapsed,
		readBytesPerSecond: elapsed <= 0 ? 0 : readBytes / elapsed,
		settings: Object.fromEntries(settings)
	};
}
