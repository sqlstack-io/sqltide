import { describe, expect, it } from 'vitest';

import { relationStatsQuery, relationStorage } from '../../../../src/lib/demo-readers/_api/storage';
import type { Storage } from '../../../../src/lib/demo-readers/_helpers/evidence-types';
import type { Query } from '../../../../src/lib/demo-readers/types';

describe('storage API', () => {
	it('given 64-bit counts as strings when storage is read then rows and bytes are numbers', async () => {
		const seen: string[] = [];
		const query: Query = async <Row>(sql: string): Promise<Row[]> => {
			seen.push(sql);
			return [{ name: 'orders', total_rows: '12', total_bytes: '3400' }] as Row[];
		};

		const storage: Map<string, Storage> = await relationStorage(query, 'shop');

		expect(storage.get('orders')).toEqual({ rows: 12, bytes: 3400 });
		expect(seen).toEqual([relationStatsQuery('shop')]);
	});
});
