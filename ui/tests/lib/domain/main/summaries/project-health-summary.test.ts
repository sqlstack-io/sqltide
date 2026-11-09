import { describe, expect, it } from 'vitest';
import { projectHealthSummary } from '$lib/domain/main/summaries/project-health-summary';
import type { Project, Source } from '$lib/domain/types';
import type { WarehouseHealth } from '$lib/warehouse-health/types';

interface IngestExpectation {
	readonly state: 'healthy' | 'behind' | 'error' | 'partial' | 'no_kafka';
	readonly notBuilt: number;
	readonly behind: number;
	readonly lagUnavailable: number;
}

interface IngestHealthTestCase {
	readonly description: string;
	readonly lags: Array<number | null>;
	readonly consumers: WarehouseHealth['kafkaConsumers'];
	readonly expected: IngestExpectation;
}

function projectFor(testCase: IngestHealthTestCase): Project {
	const sources: Source[] = testCase.lags.map(
		(lag, index) =>
			({
				name: `source_${index}`,
				kind: 'kafka',
				managedRelations: [{ kind: 'kafka_engine', name: `kafka__source_${index}`, ddl: null }],
				live: { kafkaLagMessages: lag }
			}) as unknown as Source
	);
	return {
		sources,
		models: [],
		warehouseHealth: {
			kafkaConsumers: testCase.consumers
		} as unknown as WarehouseHealth
	} as unknown as Project;
}

describe('projectHealthSummary ingest', () => {
	it.each<IngestHealthTestCase>([
		{
			description: 'fully built and polling',
			lags: [0, 0],
			consumers: {
				configuredTables: 2,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'healthy', notBuilt: 0, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'authored source not built',
			lags: [0, 0, null],
			consumers: {
				configuredTables: 3,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'healthy', notBuilt: 1, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'measured unbuilt source does not mask unavailable live lag',
			lags: [0, null, 0],
			consumers: {
				configuredTables: 3,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'partial', notBuilt: 1, behind: 0, lagUnavailable: 1 }
		},
		{
			description: 'stale lag from unbuilt source does not report behind',
			lags: [0, 0, 12],
			consumers: {
				configuredTables: 3,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'healthy', notBuilt: 1, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'materialized consumer stopped polling',
			lags: [0, 0],
			consumers: {
				configuredTables: 2,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 1,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'error', notBuilt: 0, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'materialized consumer exception',
			lags: [0],
			consumers: {
				configuredTables: 1,
				materializedTables: 1,
				materializedTableNames: ['kafka__source_0'],
				pollingTables: 1,
				exceptionTables: 1,
				stuckConsumers: []
			},
			expected: { state: 'error', notBuilt: 0, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'consumer stuck without a current exception',
			lags: [0, 0],
			consumers: {
				configuredTables: 2,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: [
					{ table: 'kafka__source_0', stuckSeconds: 7200, recentErrors: 10, latestError: 'cast failed' }
				]
			},
			expected: { state: 'error', notBuilt: 0, behind: 0, lagUnavailable: 0 }
		},
		{
			description: 'broker lag behind',
			lags: [12, 0],
			consumers: {
				configuredTables: 2,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'behind', notBuilt: 0, behind: 1, lagUnavailable: 0 }
		},
		{
			description: 'materialized lag unavailable',
			lags: [null, 0],
			consumers: {
				configuredTables: 2,
				materializedTables: 2,
				materializedTableNames: ['kafka__source_0', 'kafka__source_1'],
				pollingTables: 2,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'partial', notBuilt: 0, behind: 0, lagUnavailable: 1 }
		},
		{
			description: 'no authored Kafka sources',
			lags: [],
			consumers: {
				configuredTables: 0,
				materializedTables: 0,
				materializedTableNames: [],
				pollingTables: 0,
				exceptionTables: 0,
				stuckConsumers: []
			},
			expected: { state: 'no_kafka', notBuilt: 0, behind: 0, lagUnavailable: 0 }
		}
	])('classifies $description truthfully', (testCase) => {
		const ingest: ReturnType<typeof projectHealthSummary>['ingest'] = projectHealthSummary(
			projectFor(testCase)
		).ingest;

		expect(ingest.state).toBe(testCase.expected.state);
		expect(ingest.notBuilt).toBe(testCase.expected.notBuilt);
		expect(ingest.behind).toBe(testCase.expected.behind);
		expect(ingest.lagUnavailable).toBe(testCase.expected.lagUnavailable);
	});
});
