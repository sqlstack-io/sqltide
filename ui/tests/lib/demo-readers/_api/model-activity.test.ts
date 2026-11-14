import { describe, expect, it } from 'vitest';

import { readModelActivity } from '../../../../src/lib/demo-readers/_api/model-activity';
import type { ModelActivity } from '../../../../src/lib/demo-readers/_helpers/live-state';
import type { Query } from '../../../../src/lib/demo-readers/types';

describe('model activity API', () => {
	it('given no system logs when activity is read then recent part changes approximate it through the binding', async () => {
		const query: Query = async <Row>(sql: string): Promise<Row[]> =>
			(sql.includes("database = 'system'") ? [] : [{ table: 'orders__20260101T000000Z_a1', last_modified_at: '2026-01-01 10:00:00' }]) as Row[];

		const activity: Map<string, ModelActivity> = await readModelActivity(query, 'shop', ['orders'], '2026-01-01 10:01:00.000', new Map([['orders', 'orders__20260101T000000Z_a1']]));

		expect(activity.get('orders')).toMatchObject({ state: 'moving', source: 'system_parts', approximate: true, lastWriteAt: '2026-01-01 10:00:00' });
	});

	it('given part_log with new parts when activity is read then written rows are summed', async () => {
		const query: Query = async <Row>(sql: string): Promise<Row[]> =>
			(sql.includes("database = 'system'")
				? [{ name: 'part_log' }]
				: [{ observed_at: '2026-01-01 10:00:00.1', table: 'orders', rows: '2' }, { observed_at: '2026-01-01 10:00:00.2', table: 'orders', rows: '3' }]) as Row[];

		const activity: Map<string, ModelActivity> = await readModelActivity(query, 'shop', ['orders'], '2026-01-01 10:01:00.000', new Map());

		expect(activity.get('orders')).toMatchObject({ state: 'moving', source: 'part_log', rowsWritten: 5, detail: '5 rows were written in the last 120s.' });
	});

	it('given a failing log read when activity is read then every relation is unknown', async () => {
		const query: Query = async (): Promise<never> => {
			throw new Error('no such table');
		};

		expect((await readModelActivity(query, 'shop', ['orders'], '2026-01-01 10:01:00.000', new Map())).get('orders')?.state).toBe('unknown');
	});
});
