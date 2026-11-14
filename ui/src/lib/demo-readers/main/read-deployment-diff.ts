import { loadCatalog, inspectManagedTableState } from '../_api/catalog.ts';
import {
	columnNamesMissingFrom,
	diffStatus,
	identifierError,
	parseComparison,
	relationPairs
} from '../_helpers/deployment-diff.ts';
import type { DiffSide, RelationPair } from '../_helpers/deployment-diff.ts';
import { loadDeploymentInventory } from '../_api/inventory.ts';
import { toInt } from '../_helpers/values.ts';
import type {
	Catalog,
	CatalogColumn,
	CatalogRelation,
	DeploymentInventory,
	ManagedTableState
} from '../_helpers/evidence-types.ts';
import type { DeploymentDiffPayload, DeploymentDiffRelationPayload, Query } from '../types.ts';

async function rowCount(query: Query, database: string, relation: CatalogRelation | undefined): Promise<number | null> {
	if (relation === undefined) return null;
	const error: string | null = identifierError(database) ?? identifierError(relation.name);
	if (error !== null) throw new Error(error);
	const rows: { row_count: unknown }[] = await query(
		`SELECT count() AS row_count FROM \`${database}\`.\`${relation.name}\``
	);
	if (rows.length !== 1) throw new Error(`Deployment diff count for '${relation.name}' returned an unexpected shape`);
	return toInt(rows[0].row_count);
}

async function compareRelation(query: Query, pair: RelationPair, catalog: Catalog): Promise<DeploymentDiffRelationPayload> {
	const lookup: (name: string | null) => CatalogRelation | undefined = (name) =>
		name === null ? undefined : catalog.relations.find((relation) => relation.name === name);
	const same: boolean = pair.fromPhysicalName !== null && pair.fromPhysicalName === pair.toPhysicalName;
	const fromRelation: CatalogRelation | undefined = lookup(pair.fromPhysicalName);
	const toRelation: CatalogRelation | undefined = same ? fromRelation : lookup(pair.toPhysicalName);
	const fromRows: number | null = await rowCount(query, pair.database, fromRelation);
	const toRows: number | null = same ? fromRows : await rowCount(query, pair.database, toRelation);
	const side: (physical: string | null, relation: CatalogRelation | undefined, rows: number | null) => DiffSide = (
		physical,
		relation,
		rows
	) => ({
		bound: physical !== null,
		found: relation === undefined ? null : { columns: relation.columns, rowCount: rows ?? 0 }
	});
	const fromColumns: CatalogColumn[] = fromRelation?.columns ?? [];
	const toColumns: CatalogColumn[] = toRelation?.columns ?? [];
	return {
		logicalName: pair.logicalName,
		status: diffStatus(side(pair.fromPhysicalName, fromRelation, fromRows), side(pair.toPhysicalName, toRelation, toRows)),
		fromPhysicalName: pair.fromPhysicalName,
		toPhysicalName: pair.toPhysicalName,
		fromRowCount: fromRows,
		toRowCount: toRows,
		addedColumns: columnNamesMissingFrom(toColumns, fromColumns),
		removedColumns: columnNamesMissingFrom(fromColumns, toColumns)
	};
}

/**
 * `GET /api/deployments/{id}/diff[?against=]`: the schema and row-count comparison of one
 * deployment with the active bindings, or with `against`. Throws the refusal for an endpoint
 * that cannot be resolved.
 */
export async function readDeploymentDiff(
	query: Query,
	database: string,
	deploymentId: string,
	against: string | null = null
): Promise<DeploymentDiffPayload> {
	const identifier: string | null = identifierError(database);
	if (identifier !== null) throw new Error(identifier);
	const [from, to] = parseComparison(against === null ? deploymentId : `${against}:${deploymentId}`);
	const inventory: DeploymentInventory = await loadDeploymentInventory(query, database);
	const state: ManagedTableState = await inspectManagedTableState(query, database);
	const pairs: RelationPair[] = relationPairs(from, to, inventory.deployments, state.activeBindings, database);
	const catalogs: Map<string, Catalog> = new Map();
	for (const pairDatabase of new Set(pairs.map((pair) => pair.database))) {
		catalogs.set(pairDatabase, await loadCatalog(query, pairDatabase));
	}
	const relations: DeploymentDiffRelationPayload[] = [];
	for (const pair of pairs) {
		const catalog: Catalog | undefined = catalogs.get(pair.database);
		if (catalog === undefined) throw new Error(`no catalog for ${pair.database}`);
		relations.push(await compareRelation(query, pair, catalog));
	}
	return { database, fromEndpoint: from, toEndpoint: to, relations };
}
