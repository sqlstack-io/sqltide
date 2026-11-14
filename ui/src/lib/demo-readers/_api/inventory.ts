/**
 * The deployment inventory: persisted lifecycle records and publications from the metadata
 * tables, combined with the relations and stable bindings in the warehouse.
 */
import { DEPLOYMENT_STATUS_INCOMPLETE } from '../constants.ts';
import type { Query } from '../types.ts';
import { deploymentSummaries } from '../_helpers/lifecycle.ts';
import { compareText, pythonDatetimeText, quoteString, sortedUnique } from '../_helpers/values.ts';
import { inspectManagedTableState, loadCatalog } from './catalog.ts';
import type {
	Catalog,
	DeploymentEvidence,
	DeploymentInventory,
	DeploymentRecord,
	ManagedTableState,
	MetadataKey,
	PublishEvent
} from '../_helpers/evidence-types.ts';

const DEPLOYMENTS_TABLE: string = '_sqltide_virtual_deployments';
const OBJECT_STATE_TABLE: string = '_sqltide_virtual_object_state';
const PUBLISH_HISTORY_TABLE: string = '_sqltide_virtual_publications';
const PROGRESS_TABLE: string = '_sqltide_virtual_deployment_progress';

type DeploymentRow = { deployment_id: string; created_at: string };
type ObjectRow = {
	deployment_id: string;
	logical_database_name: string | null;
	logical_object_type: string;
	logical_object_name: string;
	physical_relation_name: string | null;
	logical_model_name: string | null;
	is_selected_root: boolean | number;
	observed_at: string;
};
type PublishRow = {
	publication_id: string;
	deployment_id: string;
	published_at: string;
	logical_database_name: string;
	logical_view_name: string;
	physical_relation_name: string;
};

async function existingTables(query: Query, database: string, names: string[]): Promise<Set<string>> {
	const rows: { name: string }[] = await query(
		`SELECT name FROM system.tables WHERE database = ${quoteString(database)} AND name IN (${names.map(quoteString).join(', ')})`
	);
	return new Set(rows.map((row) => row.name));
}

async function publishRows(query: Query, database: string): Promise<PublishRow[]> {
	const columns: { name: string }[] = await query(
		`SELECT name FROM system.columns WHERE database = ${quoteString(database)} AND table = ${quoteString(PUBLISH_HISTORY_TABLE)}`
	);
	const names: Set<string> = new Set(columns.map((column) => column.name));
	const operation: string = names.has('operation') ? 'operation' : "'promote' AS operation";
	const rows: PublishRow[] = await query<PublishRow>(
		`SELECT DISTINCT publication_id, deployment_id, ${operation}, toString(published_at) AS published_at, ` +
			`logical_database_name, logical_view_name, physical_relation_name FROM ${database}.${PUBLISH_HISTORY_TABLE}`
	);
	return rows.map((row) => ({ ...row, published_at: pythonDatetimeText(row.published_at) }));
}

function metadataKey(row: ObjectRow): MetadataKey {
	return { database: row.logical_database_name, objectType: row.logical_object_type, name: row.logical_object_name };
}

function deploymentRecord(
	row: DeploymentRow & { headerPresent: boolean },
	objects: ObjectRow[],
	publications: PublishRow[],
	unfinished: Set<string>
): DeploymentRecord {
	const owned: ObjectRow[] = objects.filter((object) => object.deployment_id === row.deployment_id);
	const status: string =
		!row.headerPresent || unfinished.has(row.deployment_id)
			? DEPLOYMENT_STATUS_INCOMPLETE
			: publications.some((event) => event.deployment_id === row.deployment_id)
				? 'published'
				: 'staged';
	return {
		deploymentId: row.deployment_id,
		createdAt: row.created_at,
		status,
		selectedRootKeys: owned.filter((object) => Boolean(object.is_selected_root)).map(metadataKey),
		mappings: owned.flatMap((object) =>
			object.physical_relation_name === null || object.logical_model_name === null
				? []
				: [
						{
							key: metadataKey(object),
							physicalName: object.physical_relation_name,
							logicalModelName: object.logical_model_name
						}
					]
		)
	};
}

/** Persisted deployment records and publish events; a missing metadata table reads as empty. */
export async function loadDeploymentInventory(query: Query, database: string): Promise<DeploymentInventory> {
	const present: Set<string> = await existingTables(query, database, [
		DEPLOYMENTS_TABLE,
		OBJECT_STATE_TABLE,
		PUBLISH_HISTORY_TABLE,
		PROGRESS_TABLE
	]);
	const deployments: (DeploymentRow & { headerPresent: boolean })[] = present.has(DEPLOYMENTS_TABLE)
		? (
				await query<DeploymentRow>(
					`SELECT deployment_id, toString(created_at) AS created_at FROM ${database}.${DEPLOYMENTS_TABLE}`
				)
			).map((row) => ({ deployment_id: row.deployment_id, created_at: pythonDatetimeText(row.created_at), headerPresent: true }))
		: [];
	const objects: ObjectRow[] = present.has(OBJECT_STATE_TABLE)
		? (
				await query<ObjectRow>(
					'SELECT DISTINCT deployment_id, logical_database_name, logical_object_type, logical_object_name, ' +
						'physical_relation_name, logical_model_name, is_selected_root, toString(observed_at) AS observed_at ' +
						`FROM ${database}.${OBJECT_STATE_TABLE} WHERE state_kind = 'deployment' AND deployment_id IS NOT NULL`
				)
			).map((row) => ({ ...row, observed_at: pythonDatetimeText(row.observed_at) }))
		: [];
	const publications: PublishRow[] = present.has(PUBLISH_HISTORY_TABLE) ? await publishRows(query, database) : [];
	// Builds that recorded their start but not their completion failed while filling their tables.
	const progress: { deployment_id: string; stage: string }[] = present.has(PROGRESS_TABLE)
		? await query(`SELECT DISTINCT deployment_id, stage FROM ${database}.${PROGRESS_TABLE}`)
		: [];
	const completed: Set<string> = new Set(
		progress.filter((row) => row.stage === 'completed').map((row) => row.deployment_id)
	);
	const unfinished: Set<string> = new Set(
		progress.filter((row) => row.stage === 'started' && !completed.has(row.deployment_id)).map((row) => row.deployment_id)
	);
	const persisted: Set<string> = new Set(deployments.map((row) => row.deployment_id));
	for (const deploymentId of sortedUnique(
		objects.filter((row) => !persisted.has(row.deployment_id)).map((row) => row.deployment_id)
	)) {
		const observed: string[] = objects
			.filter((row) => row.deployment_id === deploymentId)
			.map((row) => row.observed_at)
			.sort(compareText);
		deployments.push({ deployment_id: deploymentId, created_at: observed[0] ?? '', headerPresent: false });
	}
	const publishEvents: PublishEvent[] = sortedUnique(publications.map((row) => row.publication_id)).map((id) => {
		const rows: PublishRow[] = publications.filter((row) => row.publication_id === id);
		return {
			publicationId: id,
			deploymentId: rows[0].deployment_id,
			publishedAt: rows[0].published_at,
			logicalViewNames: rows.map((row) => row.logical_view_name),
			bindings: rows.map((row) => ({
				database: row.logical_database_name,
				logicalName: row.logical_view_name,
				physicalName: row.physical_relation_name
			}))
		};
	});
	return {
		deployments: deployments.map((row) => deploymentRecord(row, objects, publications, unfinished)),
		publishEvents
	};
}

/** The inventory, managed state and catalog of `database`, and every deployment's summary. */
export async function loadDeploymentEvidence(query: Query, database: string): Promise<DeploymentEvidence> {
	const inventory: DeploymentInventory = await loadDeploymentInventory(query, database);
	const inspected: ManagedTableState = await inspectManagedTableState(query, database);
	const catalog: Catalog = await loadCatalog(query, database);
	const existing: Set<string> = new Set(catalog.relations.map((relation) => relation.name));
	return { database, inventory, inspected, catalog, summaries: deploymentSummaries(inventory, inspected, existing) };
}
