/** The pure parts of the `/api/state` overlay: ages, freshness, buckets and model entries. */
import type { JsonObject, StateDefinitions, StatePayload } from '../types.ts';
import { round3, timestampMicros } from './values.ts';

const THROUGHPUT_WINDOW_LADDER: readonly (readonly [number, number])[] = [
	[3600, 60],
	[86400, 1800],
	[604800, 10800]
];
const SECONDS_BY_DURATION_UNIT: Readonly<Record<string, number>> = { d: 86400, h: 3600, m: 60, s: 1 };

export type RelationStats = { rows: number; bytes: number; parts: number };
export type Extent = { oldest: string | null; newest: string | null; rows: number };
/** `(partition, max offset, oldest, newest, rows)` of one landed partition. */
export type PartitionObservation = [number, number, string, string, number];
export type Throughput = { windowSeconds: number; bucketSeconds: number; buckets: number[] };

export type FreshnessPolicy = { warnAfter: string | null; errorAfter: string | null } | null;

export type ModelActivity = {
	state: 'moving' | 'idle' | 'stalled' | 'unknown';
	source: string;
	sourceAvailable: boolean;
	approximate: boolean;
	lastTriggeredAt: string | null;
	lastWriteAt: string | null;
	rowsWritten: number;
	windowSeconds: number;
	detail: string;
};

export type Measurements = {
	capturedAt: string;
	stats: Map<string, RelationStats>;
	extents: Map<string, Extent>;
	partitions: Map<string, Map<number, PartitionObservation>>;
	activity: Map<string, ModelActivity>;
};

/** The relation name the template records for one model or source. */
export function relationOf(entries: { [name: string]: JsonObject }, name: string): string {
	return String(entries[name]?.relationName);
}

/** Names of the table models; views have no storage of their own. */
export function tableModels(definitions: StateDefinitions): Set<string> {
	return new Set(definitions.models.filter((model) => model.kind === 'table').map((model) => model.name));
}

/** Fold each source relation's partitions into one extent. */
export function addSourceExtents(
	partitions: Map<string, Map<number, PartitionObservation>>,
	into: Map<string, Extent>
): void {
	for (const [relation, byPartition] of partitions) {
		const observations: PartitionObservation[] = [...byPartition.values()];
		if (observations.length === 0) continue;
		let oldest: string | null = null;
		let newest: string | null = null;
		let rows: number = 0;
		for (const [, , first, last, count] of observations) {
			if (oldest === null || first < oldest) oldest = first;
			if (newest === null || last > newest) newest = last;
			rows += count;
		}
		into.set(relation, { oldest, newest, rows });
	}
}

/** `round((captured_at - newest).total_seconds(), 3)`; `null` when either does not parse. */
export function lagSeconds(newest: string | null, capturedAt: string): number | null {
	if (newest === null) return null;
	const newestAt: number | null = timestampMicros(newest);
	const now: number | null = timestampMicros(capturedAt);
	return newestAt === null || now === null ? null : round3((now - newestAt) / 1e6);
}

function policySeconds(value: string | null): number | null {
	if (value === null) return null;
	return Number(value.slice(0, -1)) * (SECONDS_BY_DURATION_UNIT[value.slice(-1)] ?? Number.NaN);
}

export function freshness(newest: string | null, capturedAt: string, policy: FreshnessPolicy): string | null {
	if (policy === null) return null;
	const age: number | null = lagSeconds(newest, capturedAt);
	if (age === null) return 'stalled';
	const errorAfter: number | null = policySeconds(policy.errorAfter);
	if (errorAfter !== null && age > errorAfter) return 'stalled';
	const warnAfter: number | null = policySeconds(policy.warnAfter);
	if (warnAfter !== null && age > warnAfter) return 'lagging';
	return 'fresh';
}

/** The smallest ladder rung whose window still contains the newest event. */
export function throughputWindow(newestAgeSeconds: number | null): readonly [number, number] {
	const last: readonly [number, number] = THROUGHPUT_WINDOW_LADDER[THROUGHPUT_WINDOW_LADDER.length - 1];
	if (newestAgeSeconds === null) return last;
	return THROUGHPUT_WINDOW_LADDER.find(([window]) => newestAgeSeconds < window) ?? last;
}

export function zeroFilledBuckets(counts: Map<number, number>, windowSeconds: number, bucketSeconds: number): number[] {
	const bucketCount: number = Math.floor(windowSeconds / bucketSeconds);
	if (counts.size === 0) return new Array<number>(bucketCount).fill(0);
	const start: number = Math.max(...counts.keys()) - (bucketCount - 1) * bucketSeconds;
	return Array.from({ length: bucketCount }, (_, index) => counts.get(start + index * bucketSeconds) ?? 0);
}

export function rowsPerSecond(throughput: Throughput | null): number | null {
	if (throughput === null || throughput.windowSeconds === 0) return null;
	return round3(throughput.buckets.reduce((sum, count) => sum + count, 0) / throughput.windowSeconds);
}

/** Per-partition offsets of one source; broker lag is unknown without a Kafka client. */
export function partitionsPayload(observations: Map<number, PartitionObservation> | undefined): unknown[] | null {
	if (observations === undefined) return null;
	return [...observations.values()]
		.sort((left, right) => left[0] - right[0])
		.map(([partition, maxOffset, , newest]) => ({
			partition,
			maxOffset,
			newestEventAt: newest,
			committedOffset: null,
			endOffset: null,
			kafkaLagMessages: null
		}));
}

/** Each model's freshness policy: its pipeline source's, else none. */
function modelPolicies(definitions: StateDefinitions): Map<string, FreshnessPolicy> {
	const sourcePolicies: Map<string, FreshnessPolicy> = new Map(
		definitions.sources.map((source) => [source.name, source.freshness ?? null])
	);
	const policies: Map<string, FreshnessPolicy> = new Map();
	for (const pipeline of definitions.pipelines) {
		const policy: FreshnessPolicy = pipeline.sourceName === null ? null : (sourcePolicies.get(pipeline.sourceName) ?? null);
		for (const model of pipeline.models) policies.set(model, policy);
	}
	return policies;
}

/** Model entries with their warehouse fields replaced; compile-time comparisons kept. */
export function modelStates(
	template: StatePayload,
	definitions: StateDefinitions,
	measured: Measurements
): StatePayload['models'] {
	const models: Set<string> = tableModels(definitions);
	const policies: Map<string, FreshnessPolicy> = modelPolicies(definitions);
	const states: StatePayload['models'] = {};
	for (const [name, entry] of Object.entries(template.models)) {
		const isTable: boolean = models.has(name);
		const relation: string = relationOf(template.models, name);
		const stats: RelationStats | undefined = isTable ? measured.stats.get(relation) : undefined;
		const extent: Extent | undefined = isTable ? measured.extents.get(relation) : undefined;
		const newest: string | null = extent?.newest ?? null;
		states[name] = {
			...entry,
			rows: stats?.rows ?? null,
			diskBytes: stats?.bytes ?? null,
			parts: stats?.parts ?? null,
			oldestRowAt: extent?.oldest ?? null,
			newestRowAt: newest,
			lagSeconds: lagSeconds(newest, measured.capturedAt),
			freshness: freshness(newest, measured.capturedAt, isTable ? (policies.get(name) ?? null) : null),
			activity: measured.activity.get(relation) ?? null
		};
	}
	return states;
}
