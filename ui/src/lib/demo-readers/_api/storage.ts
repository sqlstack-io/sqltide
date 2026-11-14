/** Approximate rows and bytes per relation from one scan of `system.tables`. */
import type { Query } from '../types.ts';
import { quoteString } from '../_helpers/values.ts';
import type { Storage } from '../_helpers/evidence-types.ts';

type StatsRow = { name: string; total_rows: unknown; total_bytes: unknown };

/** The relation stats query of the state overlay and the deployment payloads. */
export function relationStatsQuery(database: string): string {
	return (
		'SELECT name, coalesce(total_rows, 0) AS total_rows, coalesce(total_bytes, 0) AS total_bytes ' +
		`FROM system.tables WHERE database = ${quoteString(database)}`
	);
}

/** `int(value)` for numbers; anything else counts as zero. */
function storageInt(value: unknown): number {
	const parsed: number = typeof value === 'string' && value.trim() !== '' ? Number(value) : typeof value === 'number' ? value : Number.NaN;
	return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

export async function relationStorage(query: Query, database: string): Promise<Map<string, Storage>> {
	const rows: StatsRow[] = await query<StatsRow>(relationStatsQuery(database));
	return new Map(rows.map((row) => [row.name, { rows: storageInt(row.total_rows), bytes: storageInt(row.total_bytes) }]));
}
