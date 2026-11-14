/** Deployment lifecycle states and the deployment list entries built from them. */
import { DEPLOYMENT_STATUS_INCOMPLETE } from '../constants.ts';
import type { DeploymentState, DeploymentSummaryPayload } from '../types.ts';
import type {
	DeploymentInventory,
	DeploymentRecord,
	DeploymentSummary,
	ManagedTableState,
	Storage
} from './evidence-types.ts';
import { deploymentIdOf, modelRelationNames } from './naming.ts';
import { compareText, sortedUnique } from './values.ts';

/** Values grouped by deployment, dropping pairs without one. */
function groupByDeployment(pairs: [string | null, string][]): Map<string, string[]> {
	const grouped: Map<string, string[]> = new Map();
	for (const [deploymentId, value] of pairs) {
		if (deploymentId !== null) grouped.set(deploymentId, [...(grouped.get(deploymentId) ?? []), value]);
	}
	return grouped;
}

function deploymentState(
	record: DeploymentRecord | undefined,
	mapped: string[],
	missing: string[],
	active: string[],
	publishedAt: string[]
): DeploymentState {
	if (record === undefined) return 'metadata_missing';
	if (record.status === DEPLOYMENT_STATUS_INCOMPLETE) return 'incomplete';
	if (missing.length > 0) return 'physical_missing';
	if (active.length > 0) return 'active';
	if (publishedAt.length > 0) return 'superseded';
	if (mapped.length === 0) return 'physical_missing';
	return 'staged';
}

function rootNames(record: DeploymentRecord): string[] {
	const selected: string[] = record.selectedRootKeys.map((key) => key.name);
	return sortedUnique(selected.length > 0 ? selected : record.mappings.map((mapping) => mapping.key.name));
}

/** Every deployment any evidence names, newest deployment ID first, with its lifecycle state. */
export function deploymentSummaries(
	inventory: DeploymentInventory,
	inspected: ManagedTableState,
	existingNames: Set<string>
): DeploymentSummary[] {
	const records: Map<string, DeploymentRecord> = new Map(
		inventory.deployments.map((record) => [record.deploymentId, record])
	);
	const physical: Map<string, string[]> = groupByDeployment(
		inspected.physicalCandidates.map((candidate) => [deploymentIdOf(candidate.physicalName), candidate.physicalName])
	);
	const active: Map<string, string[]> = groupByDeployment(
		inspected.activeBindings.map((binding) => [deploymentIdOf(binding.physicalName), binding.logicalName])
	);
	const publications: Map<string, string[]> = groupByDeployment(
		inventory.publishEvents.map((event) => [event.deploymentId, event.publishedAt])
	);
	const ids: string[] = sortedUnique([...records.keys(), ...physical.keys(), ...active.keys(), ...publications.keys()]);
	return ids.reverse().map((deploymentId) => {
		const record: DeploymentRecord | undefined = records.get(deploymentId);
		const mapped: string[] = sortedUnique(record?.mappings.map((mapping) => mapping.physicalName) ?? []);
		const missing: string[] = mapped.filter((name) => !existingNames.has(name));
		const activeNames: string[] = sortedUnique(active.get(deploymentId) ?? []);
		const publishedAt: string[] = publications.get(deploymentId) ?? [];
		return {
			deploymentId,
			state: deploymentState(record, mapped, missing, activeNames, publishedAt),
			createdAt: record?.createdAt ?? null,
			persistedStatus: record?.status ?? null,
			rootNames: record === undefined ? [] : rootNames(record),
			physicalRelationNames: sortedUnique([...mapped, ...(physical.get(deploymentId) ?? [])]),
			missingPhysicalRelationNames: missing,
			activeBindingNames: activeNames,
			latestPublishedAt: publishedAt.length === 0 ? null : [...publishedAt].sort(compareText)[publishedAt.length - 1]
		};
	});
}

export function storageOf(storage: Map<string, Storage>, name: string): Storage {
	return storage.get(name) ?? { rows: 0, bytes: 0 };
}

export function storageTotal(relationNames: string[], storage: Map<string, Storage>): Storage {
	return relationNames.reduce(
		(total, name) => ({ rows: total.rows + storageOf(storage, name).rows, bytes: total.bytes + storageOf(storage, name).bytes }),
		{ rows: 0, bytes: 0 }
	);
}

/** One deployment of the `/api/deployments` list. */
export function summaryPayload(summary: DeploymentSummary, storage: Map<string, Storage>): DeploymentSummaryPayload {
	const totals: Storage = storageTotal(summary.physicalRelationNames, storage);
	return {
		deploymentId: summary.deploymentId,
		state: summary.state,
		createdAt: summary.createdAt,
		publishedAt: summary.latestPublishedAt,
		persistedStatus: summary.persistedStatus,
		rootNames: summary.rootNames,
		physicalRelationNames: summary.physicalRelationNames,
		activeBindingNames: summary.activeBindingNames,
		missingRelationNames: summary.missingPhysicalRelationNames,
		modelCount: modelRelationNames(summary.physicalRelationNames).length,
		relationCount: summary.physicalRelationNames.length,
		rows: totals.rows,
		bytes: totals.bytes
	};
}
