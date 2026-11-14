import { describe, expect, it } from 'vitest';

import { sourceStates } from '../../../../src/lib/demo-readers/_api/source-states';
import type { Measurements } from '../../../../src/lib/demo-readers/_helpers/live-state';
import type { Query, StateDefinitions, StatePayload } from '../../../../src/lib/demo-readers/types';

const template: StatePayload = {
	capturedAt: '',
	warehouseHealth: null,
	models: {},
	sources: { orders: { relationName: 'raw_orders', consumerGroupDrift: null } }
};
const definitions: StateDefinitions = {
	sources: [{ name: 'orders', freshness: { warnAfter: '1m', errorAfter: '1h' } }],
	pipelines: [],
	models: []
};
const measured: Measurements = {
	capturedAt: '2026-01-01 10:10:00.000',
	stats: new Map([['raw_orders', { rows: 9, bytes: 90, parts: 1 }]]),
	extents: new Map([['raw_orders', { oldest: '2026-01-01 09:00:00.000', newest: '2026-01-01 10:00:00.000', rows: 9 }]]),
	partitions: new Map([['raw_orders', new Map([[0, [0, 8, '2026-01-01 09:00:00.000', '2026-01-01 10:00:00.000', 9]]])]]),
	activity: new Map()
};

describe('source states API', () => {
	it('given landed events ten minutes old when sources are measured then they are lagging with hourly throughput', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [{ relation: 'raw_orders', bucket: '1767261600', rows: '9' }] as Row[];

		const states: StatePayload['sources'] = await sourceStates(query, 'shop', template, definitions, measured);

		expect(states.orders).toMatchObject({
			rows: 9,
			lastArrivalSeconds: 600,
			freshness: 'lagging',
			kafkaLagMessages: null,
			throughput: { windowSeconds: 3600, bucketSeconds: 60 },
			rowsPerSecond: 0.003,
			partitions: [{ partition: 0, maxOffset: 8, newestEventAt: '2026-01-01 10:00:00.000', committedOffset: null, endOffset: null, kafkaLagMessages: null }]
		});
	});
});
