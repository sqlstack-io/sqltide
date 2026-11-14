/** Terminal invocations and run event streams read from the append-only run tables. */
import { INVOCATIONS_TABLE, RUN_EVENTS_TABLE } from '../constants.ts';
import type { Query, RunPayload } from '../types.ts';
import type { Streams } from '../_helpers/run-assembly.ts';
import { streamsFromRows, terminalRun } from '../_helpers/run-rows.ts';
import type { EventRow, InvocationRow } from '../_helpers/run-rows.ts';
import { pyStr, sqlLiteral } from '../_helpers/values.ts';
import { tableExists } from './warehouse-probes.ts';

const EVENT_COLUMNS: string =
	'invocation_id, sequence, toString(emitted_at) AS emitted_at, event_kind, step_id, phase, payload_json';

type TerminalFilter = { invocationId?: string; limit?: number; scheduled?: boolean };

type EventFilter = {
	invocationId?: string;
	after?: number;
	rowLimit?: number;
	recentLimit?: number;
	activeOnly?: boolean;
	excludeTerminalInvocations?: boolean;
};


/** Terminal invocations by ID, newest start first. */
export async function terminalRuns(
	query: Query,
	database: string,
	filter: TerminalFilter,
	present?: boolean
): Promise<Map<string, RunPayload>> {
	const runs: Map<string, RunPayload> = new Map();
	if (!(present ?? (await tableExists(query, database, INVOCATIONS_TABLE)))) return runs;
	const clauses: string[] = [];
	if (filter.invocationId !== undefined) clauses.push(`invocation_id = '${sqlLiteral(filter.invocationId)}'`);
	if (filter.scheduled === true) clauses.push("command = 'audit' AND mode = 'scheduled'");
	if (filter.scheduled === false) clauses.push("NOT (command = 'audit' AND coalesce(mode, '') = 'scheduled')");
	const where: string = clauses.length === 0 ? '' : ` WHERE ${clauses.join(' AND ')}`;
	const limit: string = filter.limit === undefined ? '' : ` LIMIT ${filter.limit}`;
	const rows: InvocationRow[] = await query<InvocationRow>(
		'SELECT invocation_id, project_identity, command, mode, outcome, exit_code, ' +
			'toString(started_at) AS started_at, toString(completed_at) AS completed_at, ' +
			'duration_ms, selected_node_count, error_message, summary_json, tool_version ' +
			`FROM \`${database}\`.\`${INVOCATIONS_TABLE}\`${where} ORDER BY started_at DESC${limit}`
	);
	for (const row of rows) runs.set(pyStr(row.invocation_id), terminalRun(row));
	return runs;
}

/** The `run_started` events of the given invocations. */
export async function runStartedStreams(
	query: Query,
	database: string,
	invocationIds: string[],
	present: boolean
): Promise<Streams> {
	if (invocationIds.length === 0 || !present) return new Map();
	const literals: string = invocationIds.map((id) => `'${sqlLiteral(id)}'`).join(', ');
	return streamsFromRows(
		await query<EventRow>(
			`SELECT ${EVENT_COLUMNS} FROM \`${database}\`.\`${RUN_EVENTS_TABLE}\` ` +
				`WHERE invocation_id IN (${literals}) AND event_kind = 'run_started' ORDER BY invocation_id, sequence`
		)
	);
}

export async function eventStreams(
	query: Query,
	database: string,
	filter: EventFilter,
	present?: boolean
): Promise<Streams> {
	if (!(present ?? (await tableExists(query, database, RUN_EVENTS_TABLE)))) return new Map();
	const events: string = `\`${database}\`.\`${RUN_EVENTS_TABLE}\``;
	const clauses: string[] = [];
	if (filter.invocationId !== undefined) clauses.push(`invocation_id = '${sqlLiteral(filter.invocationId)}'`);
	if (filter.after !== undefined && filter.after !== 0) clauses.push(`sequence > ${filter.after}`);
	if (filter.activeOnly === true) {
		clauses.push(
			`invocation_id IN (SELECT invocation_id FROM ${events} GROUP BY invocation_id ` +
				"HAVING countIf(event_kind = 'run_started') > 0 AND countIf(event_kind = 'run_completed') = 0)"
		);
	}
	if (filter.excludeTerminalInvocations === true) {
		clauses.push(`invocation_id NOT IN (SELECT invocation_id FROM \`${database}\`.\`${INVOCATIONS_TABLE}\`)`);
	}
	const where: string = clauses.length === 0 ? '' : ` WHERE ${clauses.join(' AND ')}`;
	const sql: string =
		filter.recentLimit === undefined
			? `SELECT ${EVENT_COLUMNS} FROM ${events}${where} ORDER BY invocation_id, sequence` +
				(filter.rowLimit === undefined ? '' : ` LIMIT ${filter.rowLimit}`)
			: `SELECT ${EVENT_COLUMNS} FROM (SELECT ${EVENT_COLUMNS}, ` +
				`row_number() OVER (PARTITION BY invocation_id ORDER BY sequence DESC) AS recency FROM ${events}${where}) ` +
				`WHERE recency <= ${filter.recentLimit} OR event_kind = 'run_started' ORDER BY invocation_id, sequence`;
	return streamsFromRows(await query<EventRow>(sql));
}
