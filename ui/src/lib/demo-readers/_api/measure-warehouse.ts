/** Every warehouse measurement the `/api/state` model and source entries use, read once. */
import type { Query, StateDefinitions, StatePayload } from '../types.ts';
import { addSourceExtents, relationOf, tableModels } from '../_helpers/live-state.ts';
import type {
	Extent,
	Measurements,
	ModelActivity,
	PartitionObservation,
	RelationStats
} from '../_helpers/live-state.ts';
import { pyStr, quoteString, toInt } from '../_helpers/values.ts';
import { inspectManagedTableState } from './catalog.ts';
import { readModelActivity } from './model-activity.ts';
import { relationStatsQuery } from './storage.ts';

const LANDED_AT_COLUMN: string = '_replay_landed_at';

/** Rows, bytes and active parts per relation; a stable view measures the relation it binds. */
async function relationStats(
	query: Query,
	database: string,
	bindings: [string, string][]
): Promise<Map<string, RelationStats>> {
	const stats: Map<string, RelationStats> = new Map();
	for (const row of await query<{ name: string; total_rows: unknown; total_bytes: unknown }>(relationStatsQuery(database))) {
		stats.set(row.name, { rows: toInt(row.total_rows), bytes: toInt(row.total_bytes), parts: 0 });
	}
	for (const row of await query<{ table: string; parts: unknown }>(
		`SELECT table, count() AS parts FROM system.parts WHERE database = ${quoteString(database)} AND active GROUP BY table`
	)) {
		const entry: RelationStats | undefined = stats.get(row.table);
		if (entry !== undefined) entry.parts = toInt(row.parts);
	}
	for (const [logical, physical] of bindings) {
		const measured: RelationStats | undefined = stats.get(physical);
		if (measured !== undefined) stats.set(logical, { ...measured });
	}
	return stats;
}

/** Relations carrying the landed-at lineage column, ordered by name. */
async function lineageRelationNames(query: Query, database: string): Promise<string[]> {
	const rows: { table: string }[] = await query(
		`SELECT DISTINCT table FROM system.columns WHERE database = ${quoteString(database)} ` +
			`AND name = ${quoteString(LANDED_AT_COLUMN)} ORDER BY table`
	);
	return rows.map((row) => row.table);
}

async function landedExtents(query: Query, database: string, relationNames: string[]): Promise<Map<string, Extent>> {
	if (relationNames.length === 0) return new Map();
	const sql: string = relationNames
		.map(
			(name) =>
				`SELECT '${name}' AS relation, toString(min(_replay_landed_at)) AS oldest, ` +
				`toString(max(_replay_landed_at)) AS newest, count() AS rows FROM \`${database}\`.\`${name}\` HAVING count() > 0`
		)
		.join(' UNION ALL ');
	const rows: { relation: string; oldest: string; newest: string; rows: unknown }[] = await query(sql);
	return new Map(rows.map((row) => [row.relation, { oldest: pyStr(row.oldest), newest: pyStr(row.newest), rows: toInt(row.rows) }]));
}

/** `{relation: {partition: observation}}` for every source relation. */
async function warehousePartitions(
	query: Query,
	database: string,
	relationNames: string[]
): Promise<Map<string, Map<number, PartitionObservation>>> {
	const observations: Map<string, Map<number, PartitionObservation>> = new Map(
		relationNames.map((name) => [name, new Map()])
	);
	if (relationNames.length === 0) return observations;
	const sql: string =
		relationNames
			.map(
				(name) =>
					`SELECT '${name}' AS relation, _replay_partition AS partition, max(_replay_offset) AS max_offset, ` +
					'toString(min(_replay_landed_at)) AS oldest, toString(max(_replay_landed_at)) AS newest, count() AS rows ' +
					`FROM \`${database}\`.\`${name}\` GROUP BY partition`
			)
			.join(' UNION ALL ') + ' ORDER BY relation, partition';
	const rows: { relation: string; partition: unknown; max_offset: unknown; oldest: string; newest: string; rows: unknown }[] =
		await query(sql);
	for (const row of rows) {
		const partition: number = toInt(row.partition);
		observations
			.get(row.relation)
			?.set(partition, [partition, toInt(row.max_offset), pyStr(row.oldest), pyStr(row.newest), toInt(row.rows)]);
	}
	return observations;
}

/** Read sizes, landed-event extents, partitions and activity once. */
export async function measureWarehouse(
	query: Query,
	database: string,
	capturedAt: string,
	template: StatePayload,
	definitions: StateDefinitions
): Promise<Measurements> {
	const bindings: [string, string][] = (await inspectManagedTableState(query, database)).activeBindings.map((binding) => [
		binding.logicalName,
		binding.physicalName
	]);
	const stats: Map<string, RelationStats> = await relationStats(query, database, bindings);
	const lineage: string[] = await lineageRelationNames(query, database);
	const sourceRelations: string[] = definitions.sources
		.map((source) => relationOf(template.sources, source.name))
		.filter((relation) => lineage.includes(relation));
	const partitions: Map<string, Map<number, PartitionObservation>> = await warehousePartitions(
		query,
		database,
		sourceRelations
	);
	const extents: Map<string, Extent> = await landedExtents(
		query,
		database,
		lineage.filter((name) => !sourceRelations.includes(name))
	);
	addSourceExtents(partitions, extents);
	const models: Set<string> = tableModels(definitions);
	const activity: Map<string, ModelActivity> = await readModelActivity(
		query,
		database,
		Object.keys(template.models)
			.filter((name) => models.has(name))
			.map((name) => relationOf(template.models, name)),
		capturedAt,
		new Map(bindings)
	);
	return { capturedAt, stats, extents, partitions, activity };
}
