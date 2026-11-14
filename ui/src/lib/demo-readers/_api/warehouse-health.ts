/**
 * Warehouse health a browser engine can report. Disk capacity, inodes and server memory describe
 * the machine a ClickHouse server runs on, which an in-browser engine does not have, so they are
 * reported as unavailable with the warnings the server uses for missing evidence.
 */
import type { JsonObject, Query } from '../types.ts';
import { pyStr, quoteString, toInt } from '../_helpers/values.ts';

const CAPACITY_WARNING_AVAILABLE_FRACTION: number = 0.15;
const CAPACITY_CRITICAL_AVAILABLE_FRACTION: number = 0.05;
const DEAD_LETTERS_TABLE: string = '_sqltide_dead_letters';
const DEAD_LETTER_PAYLOAD_CHARACTERS: number = 500;
/** What a browser engine cannot measure, in the words the server uses for missing evidence. */
const BROWSER_HEALTH_WARNINGS: readonly string[] = [
	'Disk capacity is unavailable.',
	'Main-path inode metrics are unavailable.',
	'Meaningful memory context is unavailable.'
];

/** Run an optional probe; a failure only adds its warning. */
async function optional<T>(warnings: string[], warning: string, probe: () => Promise<T | null>): Promise<T | null> {
	try {
		const value: T | null = await probe();
		if (value === null) warnings.push(warning);
		return value;
	} catch {
		warnings.push(warning);
		return null;
	}
}

async function deadLetters(query: Query, database: string): Promise<JsonObject[]> {
	const [route]: { target_database: string }[] = await query(
		"SELECT extract(create_table_query, 'TO `?([^.` ]+)`?\\\\.`?" +
			`${DEAD_LETTERS_TABLE}') AS target_database FROM system.tables WHERE database = ${quoteString(database)} ` +
			"AND engine = 'MaterializedView' AND target_database != '' LIMIT 1"
	);
	const target: string = route?.target_database || database;
	const present: unknown[] = await query(
		`SELECT 1 AS present FROM system.tables WHERE database = ${quoteString(target)} AND name = ${quoteString(DEAD_LETTERS_TABLE)}`
	);
	if (present.length === 0) return [];
	const rows: { model_name: string; row_count: unknown; latest_failed_columns: unknown[]; latest_payload: string; latest_quarantined_at: string }[] =
		await query(
			'SELECT model_name, count() AS row_count, argMax(failed_columns, quarantined_at) AS latest_failed_columns, ' +
				`substringUTF8(argMax(payload, quarantined_at), 1, ${DEAD_LETTER_PAYLOAD_CHARACTERS}) AS latest_payload, ` +
				`toString(max(quarantined_at)) AS latest_quarantined_at FROM ${target}.${DEAD_LETTERS_TABLE} ` +
				'WHERE quarantined_at >= now64(3) - INTERVAL 24 HOUR GROUP BY model_name ORDER BY model_name'
		);
	return rows.map((row) => ({
		modelName: pyStr(row.model_name),
		rowCount: toInt(row.row_count),
		latestFailedColumns: row.latest_failed_columns.map(pyStr),
		latestPayload: pyStr(row.latest_payload),
		latestQuarantinedAt: pyStr(row.latest_quarantined_at)
	}));
}

/**
 * The `warehouseHealth` of the state payload. `template` is the health `sqt dev` reported, which
 * supplies the adapter name and whether managed Kafka sources are configured.
 */
export async function readWarehouseHealth(
	query: Query,
	database: string,
	capturedAt: string,
	template: JsonObject | null
): Promise<JsonObject> {
	const started: number = performance.now();
	const warnings: string[] = [...BROWSER_HEALTH_WARNINGS];
	const server: { version: string; uptime_seconds: unknown } | null = await optional(
		warnings,
		'Server identity is unavailable.',
		async () => (await query<{ version: string; uptime_seconds: unknown }>('SELECT version() AS version, uptime() AS uptime_seconds'))[0] ?? null
	);
	const activity: JsonObject | null = await optional(warnings, 'Current warehouse activity is unavailable.', async () => {
		const [row]: { active_queries: unknown; active_merges: unknown; incomplete_mutations: unknown }[] = await query(
			'SELECT (SELECT count() FROM system.processes WHERE query_id != currentQueryID()) AS active_queries, ' +
				'(SELECT count() FROM system.merges) AS active_merges, ' +
				'(SELECT count() FROM system.mutations WHERE NOT is_done) AS incomplete_mutations'
		);
		return row === undefined
			? null
			: {
					activeQueries: toInt(row.active_queries),
					activeMerges: toInt(row.active_merges),
					incompleteMutations: toInt(row.incomplete_mutations)
				};
	});
	// No managed Kafka engine exists in a browser engine, so configured sources cannot be observed.
	const configured: number = Number((template?.kafkaConsumers as JsonObject | null | undefined)?.configuredTables ?? 0);
	if (configured > 0) warnings.push('Managed Kafka consumer health is unavailable.');
	const kafkaConsumers: JsonObject | null =
		configured > 0
			? null
			: { configuredTables: 0, materializedTables: 0, materializedTableNames: [], pollingTables: 0, exceptionTables: 0, stuckConsumers: [] };
	const letters: JsonObject[] | null = await optional(warnings, 'Dead-letter counts are unavailable.', () =>
		deadLetters(query, database)
	);
	const tables: JsonObject[] | null = await optional(warnings, 'Project table footprint is unavailable.', async () =>
		(
			await query<{ table: string; rows: unknown; bytes_on_disk: unknown; active_parts: unknown }>(
				'SELECT table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes_on_disk, count() AS active_parts ' +
					`FROM system.parts WHERE active AND database = ${quoteString(database)} GROUP BY table ` +
					'ORDER BY bytes_on_disk DESC, table LIMIT 10'
			)
		).map((row) => ({
			name: pyStr(row.table),
			rows: toInt(row.rows),
			bytesOnDisk: toInt(row.bytes_on_disk),
			activeParts: toInt(row.active_parts)
		}))
	);
	return {
		availability: 'partial',
		status: 'unknown',
		adapter: template?.adapter ?? 'clickhouse',
		database,
		version: server === null ? null : pyStr(server.version),
		uptimeSeconds: server === null ? null : toInt(server.uptime_seconds),
		measuredAt: capturedAt,
		collectionDurationMs: Math.round(performance.now() - started),
		stale: false,
		warnings,
		capacityWarningFraction: CAPACITY_WARNING_AVAILABLE_FRACTION,
		capacityCriticalFraction: CAPACITY_CRITICAL_AVAILABLE_FRACTION,
		disks: [],
		inodes: { total: null, free: null, status: 'unknown' },
		memory: null,
		activity,
		kafkaConsumers,
		deadLetters: letters,
		tables
	};
}
