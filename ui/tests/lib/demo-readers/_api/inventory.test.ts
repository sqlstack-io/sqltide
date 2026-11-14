import { describe, expect, it } from 'vitest';

import { loadDeploymentEvidence, loadDeploymentInventory } from '../../../../src/lib/demo-readers/_api/inventory';
import type { DeploymentEvidence, DeploymentInventory } from '../../../../src/lib/demo-readers/_helpers/evidence-types';
import type { Query } from '../../../../src/lib/demo-readers/types';

const D1: string = '20260101T000000Z_a1';
const D2: string = '20260102T000000Z_b2';

const object = (deploymentId: string): object => ({
	deployment_id: deploymentId,
	logical_database_name: null,
	logical_object_type: 'table',
	logical_object_name: 'orders',
	physical_relation_name: `orders__${deploymentId}`,
	logical_model_name: 'orders',
	is_selected_root: true,
	observed_at: '2026-01-01 00:00:00.000'
});

const responses: [string, object[]][] = [
	['name IN (', [{ name: '_sqltide_virtual_deployments' }, { name: '_sqltide_virtual_object_state' }, { name: '_sqltide_virtual_publications' }, { name: '_sqltide_virtual_deployment_progress' }]],
	['FROM system.columns WHERE database', [{ name: 'operation' }]],
	['_sqltide_virtual_deployments', [{ deployment_id: D1, created_at: '2026-01-01 00:00:00.000' }, { deployment_id: D2, created_at: '2026-01-02 00:00:00.500' }]],
	['_sqltide_virtual_object_state', [object(D1), object(D2)]],
	['_sqltide_virtual_publications', [{ publication_id: 'p1', deployment_id: D1, published_at: '2026-01-01 01:00:00.000', logical_database_name: 'shop', logical_view_name: 'orders', physical_relation_name: `orders__${D1}` }]],
	['_sqltide_virtual_deployment_progress', [{ deployment_id: D2, stage: 'started' }]],
	["engine = 'View'", [{ name: 'orders', as_select: `SELECT * FROM shop.orders__${D1}` }]],
	["engine != 'View'", [{ name: `orders__${D1}` }, { name: `orders__${D2}` }]],
	['system.tables', [{ name: 'orders', engine: 'View', create_table_query: '', as_select: `SELECT * FROM shop.orders__${D1}` }, { name: `orders__${D1}`, engine: 'MergeTree', create_table_query: '', as_select: '' }, { name: `orders__${D2}`, engine: 'MergeTree', create_table_query: '', as_select: '' }]]
];

const query: Query = async <Row>(sql: string): Promise<Row[]> =>
	(responses.find(([pattern]) => sql.includes(pattern))?.[1] ?? []) as Row[];

describe('deployment inventory API', () => {
	it('given a started build without completion when the inventory loads then it is incomplete and the published one is published', async () => {
		const inventory: DeploymentInventory = await loadDeploymentInventory(query, 'shop');

		expect(inventory.deployments.map((record) => [record.deploymentId, record.status, record.createdAt])).toEqual([
			[D1, 'published', '2026-01-01 00:00:00'],
			[D2, 'incomplete', '2026-01-02 00:00:00.500000']
		]);
		expect(inventory.publishEvents[0].bindings).toEqual([{ database: 'shop', logicalName: 'orders', physicalName: `orders__${D1}` }]);
	});

	it('given warehouse evidence when summarized then states are classified newest deployment first', async () => {
		const evidence: DeploymentEvidence = await loadDeploymentEvidence(query, 'shop');

		expect(evidence.summaries.map((summary) => [summary.deploymentId, summary.state])).toEqual([
			[D2, 'incomplete'],
			[D1, 'active']
		]);
	});
});
