import { describe, expect, it } from 'vitest';

import { captureWarehouseTimestamp, tableExists } from '../../../../src/lib/demo-readers/_api/warehouse-probes';
import type { Query } from '../../../../src/lib/demo-readers/types';

describe('warehouse probes', () => {
	it('given a present count as a string when a table is probed then it exists', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [{ present: '1' }] as Row[];

		expect(await tableExists(query, 'shop', '_sqltide_invocations')).toBe(true);
	});

	it('given the warehouse clock when captured then its UTC text is returned', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [{ warehouse_timestamp: '2026-01-01 10:00:00.123' }] as Row[];

		expect(await captureWarehouseTimestamp(query)).toBe('2026-01-01 10:00:00.123');
	});
});
