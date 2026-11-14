/**
 * Explainable model activity from ClickHouse system logs. `query_views_log` and `part_log` may be
 * absent (a browser engine has neither); without both, recent part modifications approximate it.
 */
import type { Query } from '../types.ts';
import type { ModelActivity } from '../_helpers/live-state.ts';
import { sqlLiteral, timestampMicros, toInt } from '../_helpers/values.ts';

type Observation = {
	lastTriggeredAt: string | null;
	lastWriteAt: string | null;
	rowsWritten: number;
	latestStatus: string | null;
};

const ACTIVITY_WINDOW_SECONDS: number = 120;
const EMPTY: Observation = { lastTriggeredAt: null, lastWriteAt: null, rowsWritten: 0, latestStatus: null };
const SUCCESS_STATUS: string = 'QueryFinish';
const FAILURE_STATUSES: readonly string[] = ['ExceptionBeforeStart', 'ExceptionWhileProcessing'];

function payload(
	state: ModelActivity['state'],
	source: string,
	sourceAvailable: boolean,
	approximate: boolean,
	detail: string,
	observation: Observation = EMPTY
): ModelActivity {
	return {
		state,
		source,
		sourceAvailable,
		approximate,
		lastTriggeredAt: observation.lastTriggeredAt,
		lastWriteAt: observation.lastWriteAt,
		rowsWritten: observation.rowsWritten,
		windowSeconds: ACTIVITY_WINDOW_SECONDS,
		detail
	};
}

function writePayload(observation: Observation, source: string): ModelActivity {
	const moving: boolean = observation.rowsWritten > 0;
	return payload(
		moving ? 'moving' : 'idle',
		source,
		true,
		false,
		moving
			? `${observation.rowsWritten} rows were written in the last ${ACTIVITY_WINDOW_SECONDS}s.`
			: `The model was triggered but wrote no rows in the last ${ACTIVITY_WINDOW_SECONDS}s.`,
		observation
	);
}

function unqualifiedRelation(value: string): string {
	const unquoted: string = value.replaceAll('`', '');
	const at: number = unquoted.lastIndexOf('.');
	return at < 0 ? unquoted : unquoted.slice(at + 1);
}

async function queryViewObservations(query: Query, database: string): Promise<Map<string, Observation>> {
	const rows: { observed_at: string; view_target: string; written_rows: unknown; status: string }[] = await query(
		'SELECT toString(event_time_microseconds) AS observed_at, view_target, written_rows, toString(status) AS status ' +
			`FROM system.query_views_log WHERE event_time >= now() - INTERVAL ${ACTIVITY_WINDOW_SECONDS} SECOND ` +
			`AND view_type = 'Materialized' AND startsWith(replaceAll(view_target, '\`', ''), '${sqlLiteral(database)}.') ` +
			'ORDER BY event_time_microseconds'
	);
	const observations: Map<string, Observation> = new Map();
	for (const row of rows) {
		const name: string = unqualifiedRelation(row.view_target);
		const previous: Observation = observations.get(name) ?? EMPTY;
		const written: number = toInt(row.written_rows);
		const succeeded: boolean = row.status === SUCCESS_STATUS;
		observations.set(name, {
			lastTriggeredAt: row.observed_at,
			lastWriteAt: succeeded && written > 0 ? row.observed_at : previous.lastWriteAt,
			rowsWritten: succeeded ? previous.rowsWritten + written : previous.rowsWritten,
			latestStatus: row.status
		});
	}
	return observations;
}

async function partLogObservations(query: Query, database: string): Promise<Map<string, Observation>> {
	const rows: { observed_at: string; table: string; rows: unknown }[] = await query(
		'SELECT toString(event_time_microseconds) AS observed_at, table, rows FROM system.part_log ' +
			`WHERE database = '${sqlLiteral(database)}' AND event_type = 'NewPart' ` +
			`AND event_time >= now() - INTERVAL ${ACTIVITY_WINDOW_SECONDS} SECOND ORDER BY event_time_microseconds`
	);
	const observations: Map<string, Observation> = new Map();
	for (const row of rows) {
		const previous: Observation = observations.get(row.table) ?? EMPTY;
		const written: number = toInt(row.rows);
		observations.set(row.table, {
			lastTriggeredAt: row.observed_at,
			lastWriteAt: written > 0 ? row.observed_at : previous.lastWriteAt,
			rowsWritten: previous.rowsWritten + written,
			latestStatus: SUCCESS_STATUS
		});
	}
	return observations;
}

async function partsObservations(query: Query, database: string): Promise<Map<string, Observation>> {
	const rows: { table: string; last_modified_at: string }[] = await query(
		'SELECT table, toString(max(modification_time)) AS last_modified_at FROM system.parts ' +
			`WHERE active AND database = '${sqlLiteral(database)}' GROUP BY table ORDER BY table`
	);
	return new Map(
		rows.map((row) => [
			row.table,
			{ lastTriggeredAt: null, lastWriteAt: row.last_modified_at, rowsWritten: 0, latestStatus: SUCCESS_STATUS }
		])
	);
}

function approximatePayload(observation: Observation, capturedAt: string): ModelActivity {
	const written: number | null = observation.lastWriteAt === null ? null : timestampMicros(observation.lastWriteAt);
	const now: number | null = timestampMicros(capturedAt);
	const moving: boolean =
		written !== null && now !== null && Math.max((now - written) / 1e6, 0) <= ACTIVITY_WINDOW_SECONDS;
	return payload(
		moving ? 'moving' : 'idle',
		'system_parts',
		true,
		true,
		moving
			? 'A table part changed recently; this may include background merges.'
			: 'No recent table-part modification was observed.',
		observation
	);
}

/** Capability-aware activity evidence for every model relation, keyed by relation name. */
export async function readModelActivity(
	query: Query,
	database: string,
	relationNames: string[],
	capturedAt: string,
	activeBindings: Map<string, string>
): Promise<Map<string, ModelActivity>> {
	const unknown: ModelActivity = payload('unknown', 'unavailable', false, false, 'ClickHouse activity telemetry is unavailable.');
	let capabilities: string[];
	let views: Map<string, Observation>;
	let parts: Map<string, Observation>;
	let approximate: Map<string, Observation>;
	try {
		const rows: { name: string }[] = await query(
			"SELECT name FROM system.tables WHERE database = 'system' AND name IN ('part_log', 'query_views_log') ORDER BY name"
		);
		capabilities = [...new Set(rows.map((row) => row.name))];
		views = capabilities.includes('query_views_log') ? await queryViewObservations(query, database) : new Map();
		parts = capabilities.includes('part_log') ? await partLogObservations(query, database) : new Map();
		approximate = capabilities.length === 0 ? await partsObservations(query, database) : new Map();
	} catch {
		return new Map(relationNames.map((name) => [name, { ...unknown }]));
	}
	const activity: Map<string, ModelActivity> = new Map();
	for (const relationName of relationNames) {
		const evidence: string = activeBindings.get(relationName) ?? relationName;
		const find: (observations: Map<string, Observation>) => Observation | undefined = (observations) =>
			observations.get(evidence) ?? observations.get(relationName);
		const view: Observation | undefined = find(views);
		const part: Observation | undefined = find(parts);
		const modified: Observation | undefined = find(approximate);
		let result: ModelActivity;
		if (view !== undefined) {
			result =
				view.latestStatus !== null && FAILURE_STATUSES.includes(view.latestStatus)
					? payload('stalled', 'query_views_log', true, false, 'The latest materialized-view execution failed.', view)
					: writePayload(view, 'query_views_log');
		} else if (part !== undefined) result = writePayload(part, 'part_log');
		else if (modified !== undefined) result = approximatePayload(modified, capturedAt);
		else if (capabilities.length > 0) {
			result = payload(
				'idle',
				capabilities.includes('query_views_log') ? 'query_views_log' : 'part_log',
				true,
				false,
				`No model writes were observed in the last ${ACTIVITY_WINDOW_SECONDS}s.`
			);
		} else result = { ...unknown };
		activity.set(relationName, result);
	}
	return activity;
}
