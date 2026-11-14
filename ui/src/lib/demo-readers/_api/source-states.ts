/** The source entries of `/api/state` with their warehouse fields measured again. */
import type { Query, StateDefinitions, StatePayload } from '../types.ts';
import {
	freshness,
	lagSeconds,
	partitionsPayload,
	relationOf,
	rowsPerSecond,
	throughputWindow,
	zeroFilledBuckets
} from '../_helpers/live-state.ts';
import type { Extent, FreshnessPolicy, Measurements, Throughput } from '../_helpers/live-state.ts';
import { toInt } from '../_helpers/values.ts';

/** Landed-event counts per source relation, bucketed over a window chosen by its newest age. */
async function landedThroughputs(
	query: Query,
	database: string,
	newestAges: Map<string, number | null>
): Promise<Map<string, Throughput>> {
	const result: Map<string, Throughput> = new Map();
	if (newestAges.size === 0) return result;
	const windows: [string, number, number][] = [...newestAges].map(([name, age]) => [name, ...throughputWindow(age)]);
	const sql: string =
		windows
			.map(
				([name, window, bucket]) =>
					`SELECT '${name}' AS relation, toUnixTimestamp(toStartOfInterval(_replay_landed_at, INTERVAL ${bucket} SECOND)) AS bucket, ` +
					`count() AS rows FROM \`${database}\`.\`${name}\` WHERE _replay_landed_at >= now64(3) - INTERVAL ${window} SECOND GROUP BY bucket`
			)
			.join(' UNION ALL ') + ' ORDER BY relation, bucket';
	const counts: Map<string, Map<number, number>> = new Map();
	for (const row of await query<{ relation: string; bucket: unknown; rows: unknown }>(sql)) {
		const byBucket: Map<number, number> = counts.get(row.relation) ?? new Map();
		counts.set(row.relation, byBucket);
		byBucket.set(toInt(row.bucket), toInt(row.rows));
	}
	for (const [name, window, bucket] of windows) {
		result.set(name, { windowSeconds: window, bucketSeconds: bucket, buckets: zeroFilledBuckets(counts.get(name) ?? new Map(), window, bucket) });
	}
	return result;
}

/** Source entries with their warehouse fields replaced; broker lag is unknown without a broker. */
export async function sourceStates(
	query: Query,
	database: string,
	template: StatePayload,
	definitions: StateDefinitions,
	measured: Measurements
): Promise<StatePayload['sources']> {
	const ages: Map<string, number | null> = new Map();
	for (const source of definitions.sources) {
		const relation: string = relationOf(template.sources, source.name);
		const extent: Extent | undefined = measured.extents.get(relation);
		if (extent !== undefined) ages.set(relation, lagSeconds(extent.newest, measured.capturedAt));
	}
	const throughputs: Map<string, Throughput> = await landedThroughputs(query, database, ages);
	const states: StatePayload['sources'] = {};
	for (const [name, entry] of Object.entries(template.sources)) {
		const relation: string = relationOf(template.sources, name);
		const newest: string | null = measured.extents.get(relation)?.newest ?? null;
		const throughput: Throughput | null = throughputs.get(relation) ?? null;
		const policy: FreshnessPolicy = definitions.sources.find((source) => source.name === name)?.freshness ?? null;
		states[name] = {
			...entry,
			rows: measured.stats.get(relation)?.rows ?? null,
			oldestEventAt: measured.extents.get(relation)?.oldest ?? null,
			newestEventAt: newest,
			lastArrivalSeconds: lagSeconds(newest, measured.capturedAt),
			kafkaLagMessages: null,
			freshness: freshness(newest, measured.capturedAt, policy),
			throughput,
			rowsPerSecond: rowsPerSecond(throughput),
			partitions: partitionsPayload(measured.partitions.get(relation))
		};
	}
	return states;
}
