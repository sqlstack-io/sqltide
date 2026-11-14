import { describe, expect, it } from 'vitest';

import { inspectManagedTableState, loadCatalog } from '../../../../src/lib/demo-readers/_api/catalog';
import type { Catalog, ManagedTableState } from '../../../../src/lib/demo-readers/_helpers/evidence-types';
import type { Query } from '../../../../src/lib/demo-readers/types';

const tables: object[] = [
	{ name: 'mv__orders__20260101T000000Z_a1', engine: 'MaterializedView', create_table_query: 'CREATE MATERIALIZED VIEW shop.mv__orders__20260101T000000Z_a1 TO shop.orders__20260101T000000Z_a1 AS SELECT * FROM shop.raw_orders', as_select: 'SELECT * FROM shop.raw_orders' },
	{ name: 'orders', engine: 'View', create_table_query: 'CREATE VIEW shop.orders AS SELECT * FROM shop.orders__20260101T000000Z_a1', as_select: 'SELECT * FROM shop.orders__20260101T000000Z_a1' },
	{ name: 'orders__20260101T000000Z_a1', engine: 'MergeTree', create_table_query: 'CREATE TABLE shop.orders__20260101T000000Z_a1 (id UInt64) ENGINE = MergeTree ORDER BY id', as_select: '' }
];

const query: Query = async <Row>(sql: string): Promise<Row[]> =>
	(sql.includes('system.columns')
		? [{ table: 'orders__20260101T000000Z_a1', name: 'id', type: 'UInt64', default_expression: '' }]
		: sql.includes("engine = 'View'")
			? tables.filter((row) => (row as { engine: string }).engine === 'View')
			: sql.includes("engine != 'View'")
				? tables.filter((row) => (row as { engine: string }).engine !== 'View')
				: tables) as Row[];

describe('catalog API', () => {
	it('given system tables when the catalog loads then view bindings, writer targets and sources are derived', async () => {
		const catalog: Catalog = await loadCatalog(query, 'shop');

		expect(catalog.relations.map((relation) => [relation.name, relation.stableBindingName, relation.targetRelationName])).toEqual([
			['mv__orders__20260101T000000Z_a1', null, 'orders__20260101T000000Z_a1'],
			['orders', 'orders__20260101T000000Z_a1', null],
			['orders__20260101T000000Z_a1', null, null]
		]);
		expect(catalog.relations[0].sourceRelationNames).toEqual(['raw_orders']);
		expect(catalog.relations[2].columns).toEqual([{ name: 'id', type: 'UInt64', defaultExpression: null }]);
	});

	it('given a stable view and deployment tables when inspected then bindings and model candidates are reported', async () => {
		const state: ManagedTableState = await inspectManagedTableState(query, 'shop');

		expect(state.activeBindings).toEqual([{ database: 'shop', logicalName: 'orders', physicalName: 'orders__20260101T000000Z_a1' }]);
		expect(state.physicalCandidates).toEqual([
			{ database: 'shop', logicalName: 'orders', physicalName: 'orders__20260101T000000Z_a1', objectType: 'table' }
		]);
	});
});
