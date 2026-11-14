import { describe, expect, it } from 'vitest';

import { readWarehouseHealth } from '../../../../src/lib/demo-readers/_api/warehouse-health';
import type { JsonObject, Query } from '../../../../src/lib/demo-readers/types';

const responses: [string, object[]][] = [
	['version()', [{ version: '26.5.1', uptime_seconds: '42' }]],
	['active_queries', [{ active_queries: '1', active_merges: '0', incomplete_mutations: '0' }]],
	['system.parts', [{ table: 'orders', rows: '5', bytes_on_disk: '900', active_parts: '1' }]]
];

const query: Query = async <Row>(sql: string): Promise<Row[]> =>
	(responses.find(([pattern]) => sql.includes(pattern))?.[1] ?? []) as Row[];

describe('warehouse health API', () => {
	it('given a browser engine when health is read then host diagnostics are unavailable and the rest is measured', async () => {
		const health: JsonObject = await readWarehouseHealth(query, 'shop', '2026-01-01 10:00:00.000', { adapter: 'clickhouse' });

		expect(health).toMatchObject({
			availability: 'partial',
			status: 'unknown',
			version: '26.5.1',
			uptimeSeconds: 42,
			measuredAt: '2026-01-01 10:00:00.000',
			disks: [],
			inodes: { total: null, free: null, status: 'unknown' },
			memory: null,
			activity: { activeQueries: 1, activeMerges: 0, incompleteMutations: 0 },
			deadLetters: [],
			tables: [{ name: 'orders', rows: 5, bytesOnDisk: 900, activeParts: 1 }]
		});
		expect(health.warnings).toContain('Disk capacity is unavailable.');
	});
});
