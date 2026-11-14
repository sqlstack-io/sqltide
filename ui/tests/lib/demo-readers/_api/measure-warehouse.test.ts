import { describe, expect, it } from 'vitest';

import { measureWarehouse } from '../../../../src/lib/demo-readers/_api/measure-warehouse';
import type { Measurements } from '../../../../src/lib/demo-readers/_helpers/live-state';
import type { Query, StateDefinitions, StatePayload } from '../../../../src/lib/demo-readers/types';

const responses: [string, object[]][] = [
	["engine = 'View'", [{ name: 'orders', as_select: 'SELECT * FROM shop.orders__20260101T000000Z_a1' }]],
	['coalesce(total_rows', [{ name: 'orders__20260101T000000Z_a1', total_rows: '7', total_bytes: '700' }]],
	['count() AS parts', [{ table: 'orders__20260101T000000Z_a1', parts: '2' }]],
	["database = 'system'", [{ name: 'part_log' }]]
];

const query: Query = async <Row>(sql: string): Promise<Row[]> =>
	(responses.find(([pattern]) => sql.includes(pattern))?.[1] ?? []) as Row[];

const template: StatePayload = { capturedAt: '', warehouseHealth: null, models: { orders: { relationName: 'orders' } }, sources: {} };
const definitions: StateDefinitions = { sources: [], pipelines: [], models: [{ name: 'orders', kind: 'table' }] };

describe('warehouse measurement API', () => {
	it('given a stable view when the warehouse is measured then it reports the bound relation stats', async () => {
		const measured: Measurements = await measureWarehouse(query, 'shop', '2026-01-01 10:00:00.000', template, definitions);

		expect(measured.stats.get('orders')).toEqual({ rows: 7, bytes: 700, parts: 2 });
		expect(measured.activity.get('orders')?.state).toBe('idle');
	});
});
