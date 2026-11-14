/**
 * What promoting a deployment would do: the bindings it writes, the stale stable names it drops,
 * the live effects, and why promotion would be refused.
 */
import { PUBLISHED_OBJECT_TYPES } from '../constants.ts';
import { compareText } from './values.ts';
import type {
	Binding,
	BindingRequest,
	DeploymentEvidence,
	DeploymentRecord,
	PreparedMapping,
	PromotionPreview
} from './evidence-types.ts';
import { isDeploymentId, splitPhysicalName } from './naming.ts';

type Staged = { database: string | null; objectType: string; logicalName: string; physicalName: string };

/** Why a deployment whose build has not filled every table cannot be promoted. */
export function incompleteMessage(deploymentId: string): string {
	return (
		`Deployment ${deploymentId} has not finished building (its build failed or is still running), ` +
		'so it cannot be promoted. Promote a build once it finishes, by the ID it prints.'
	);
}

/** One `(database, name)` pair as a map key. */
function key(database: string, name: string): string {
	return `${database}\u0000${name}`;
}

function noRelations(deploymentId: string): string {
	return `Deployment '${deploymentId}' has no staged model relations to publish`;
}

/** Tables before views within each database, the default database (`null`) first. */
function compareStaged(left: Staged, right: Staged): number {
	return (
		compareText(left.database ?? '', right.database ?? '') ||
		Number(left.objectType !== 'table') - Number(right.objectType !== 'table') ||
		compareText(left.logicalName, right.logicalName)
	);
}

function publishBindings(relations: Staged[], existing: Set<string>, database: string, deploymentId: string): Binding[] {
	const missing: string[] = relations.map((relation) => relation.physicalName).filter((name) => !existing.has(name));
	if (missing.length > 0) {
		throw new Error(`Deployment '${deploymentId}' is missing staged relations: ${missing.sort(compareText).join(', ')}`);
	}
	const published: Staged[] = relations.filter((relation) => PUBLISHED_OBJECT_TYPES.includes(relation.objectType));
	if (published.length === 0) throw new Error(noRelations(deploymentId));
	return published.sort(compareStaged).map((relation) => ({
		database: relation.database ?? database,
		logicalName: relation.logicalName,
		physicalName: relation.physicalName
	}));
}

function bindingDatabase(mapping: PreparedMapping, database: string): string {
	return mapping.key.database || database;
}

/** Stable names an earlier publication of a current model bound that this one no longer binds. */
function obsoleteRemovals(
	current: PreparedMapping[],
	evidence: DeploymentEvidence
): { database: string; logicalName: string }[] {
	const { database, inventory, inspected } = evidence;
	const publishing: PreparedMapping[] = current.filter((mapping) => PUBLISHED_OBJECT_TYPES.includes(mapping.key.objectType));
	const currentNames: Set<string> = new Set(publishing.map((mapping) => `${bindingDatabase(mapping, database)}\u0000${mapping.key.name}`));
	const currentModels: Set<string> = new Set(publishing.map((mapping) => mapping.logicalModelName));
	const published: Map<string, Set<string>> = new Map();
	for (const event of inventory.publishEvents) {
		published.set(event.deploymentId, new Set([...(published.get(event.deploymentId) ?? []), ...event.logicalViewNames]));
	}
	const historical: Set<string> = new Set();
	for (const deployment of inventory.deployments) {
		const names: Set<string> | undefined = published.get(deployment.deploymentId);
		for (const mapping of deployment.mappings) {
			const name: string = `${bindingDatabase(mapping, database)}\u0000${mapping.key.name}`;
			if (currentModels.has(mapping.logicalModelName) && names?.has(mapping.key.name) && !currentNames.has(name)) {
				historical.add(`${name}\u0000${mapping.physicalName}`);
			}
		}
	}
	const active: Set<string> = new Set(
		inspected.activeBindings.map((binding) => `${binding.database}\u0000${binding.logicalName}\u0000${binding.physicalName}`)
	);
	return [...historical]
		.sort(compareText)
		.filter((binding) => active.has(binding))
		.map((binding) => {
			const [bindingDb, logicalName] = binding.split('\u0000');
			return { database: bindingDb, logicalName };
		});
}

/** The validated bindings, and the stale bindings they retire, for publishing `deploymentId`. */
export function buildPublishBindingRequest(evidence: DeploymentEvidence, deploymentId: string): BindingRequest {
	const { database, inventory, inspected, catalog } = evidence;
	const existing: Set<string> = new Set(catalog.relations.map((relation) => relation.name));
	const record: DeploymentRecord | undefined = inventory.deployments.find((item) => item.deploymentId === deploymentId);
	if (record === undefined) {
		const suffix: string = `__${deploymentId}`;
		const candidates: Staged[] = inspected.physicalCandidates
			.filter((candidate) => candidate.physicalName.endsWith(suffix))
			.map((candidate) => ({ ...candidate, database: candidate.database }));
		if (candidates.length === 0) throw new Error(noRelations(deploymentId));
		return { bindings: publishBindings(candidates, existing, database, deploymentId), removals: [] };
	}
	if (record.status === 'incomplete') throw new Error(incompleteMessage(deploymentId));
	const relations: Staged[] = record.mappings.map((mapping) => ({
		database: mapping.key.database || null,
		objectType: mapping.key.objectType,
		logicalName: mapping.key.name,
		physicalName: mapping.physicalName
	}));
	return {
		bindings: publishBindings(relations, existing, database, deploymentId),
		removals: obsoleteRemovals(record.mappings, evidence)
	};
}

/** The live effects of applying `request` over the `active` bindings. */
export function replacementPreview(request: BindingRequest, active: Binding[]): PromotionPreview {
	const activeByName: Map<string, string> = new Map(
		active.map((binding) => [key(binding.database, binding.logicalName), binding.physicalName])
	);
	const preview: PromotionPreview = { initialPublish: false, additions: [], replacements: [], removals: [], orphaned: [] };
	for (const binding of request.bindings) {
		const current: string | undefined = activeByName.get(key(binding.database, binding.logicalName));
		if (current === undefined) preview.additions.push(binding);
		else if (current !== binding.physicalName) preview.replacements.push({ binding, fromPhysicalName: current });
	}
	for (const removal of request.removals) {
		const current: string | undefined = activeByName.get(key(removal.database, removal.logicalName));
		if (current !== undefined) preview.removals.push({ ...removal, physicalName: current });
	}
	const finalByName: Map<string, string> = new Map(activeByName);
	for (const binding of request.bindings) finalByName.set(key(binding.database, binding.logicalName), binding.physicalName);
	for (const removal of request.removals) finalByName.delete(key(removal.database, removal.logicalName));
	const remaining: Set<string> = new Set(
		[...finalByName].map(([name, physical]) => key(name.split('\u0000')[0], physical))
	);
	const orphaned: Set<string> = new Set(
		active.map((binding) => key(binding.database, binding.physicalName)).filter((relation) => !remaining.has(relation))
	);
	preview.orphaned = [...orphaned].sort(compareText).map((relation) => {
		const [relationDb, physicalName] = relation.split('\u0000');
		return { database: relationDb, physicalName };
	});
	preview.initialPublish =
		preview.additions.length > 0 && preview.additions.length === request.bindings.length && request.removals.length === 0;
	return preview;
}

/**
 * Why promoting must be refused when a relation of the deployment reads another deployment's
 * physical table that will not be its upstream's published version afterwards.
 */
export function staleUpstreamMessage(
	evidence: DeploymentEvidence,
	deploymentId: string,
	request: BindingRequest
): string | null {
	const { database, catalog, inventory } = evidence;
	const published: Map<string, string> = new Map();
	for (const relation of catalog.relations) {
		if (relation.stableBindingName !== null) published.set(relation.name, relation.stableBindingName);
	}
	const own: Set<string> = new Set();
	for (const binding of request.bindings) {
		if (binding.database !== database) continue;
		published.set(binding.logicalName, binding.physicalName);
		own.add(binding.physicalName);
	}
	for (const removal of request.removals) {
		if (removal.database === database) published.delete(removal.logicalName);
	}
	const stale: [string, string, string | undefined][] = [];
	for (const relation of catalog.relations) {
		const target: string | null = relation.targetRelationName;
		const reader: string | null =
			target !== null && own.has(target) ? target : own.has(relation.name) ? relation.name : null;
		if (reader === null) continue;
		for (const source of relation.sourceRelationNames) {
			const parts: [string, string] | null = splitPhysicalName(source);
			if (parts === null || own.has(source) || !isDeploymentId(parts[1])) continue;
			const current: string | undefined = published.get(parts[0]);
			if (current !== source) stale.push([reader, source, current]);
		}
	}
	if (stale.length === 0) return null;
	const models: Map<string, string> = new Map();
	for (const deployment of inventory.deployments) {
		if (deployment.deploymentId !== deploymentId) continue;
		for (const mapping of deployment.mappings) models.set(mapping.physicalName, mapping.logicalModelName);
	}
	const model: (reader: string) => string = (reader) => models.get(reader) ?? splitPhysicalName(reader)?.[0] ?? reader;
	const lines: string[] = [];
	const selected: string[] = [];
	for (const [reader, source, current] of stale) {
		const logical: string = splitPhysicalName(source)?.[0] ?? source;
		const now: string = current === undefined ? `${logical} is not published` : `${logical} now reads ${current}`;
		lines.push(`- ${model(reader)} reads ${source}, but ${now}`);
		if (!selected.includes(model(reader))) selected.push(model(reader));
	}
	return (
		`Deployment ${deploymentId} reads an upstream version that is no longer published, so it cannot be promoted:\n` +
		`${lines.join('\n')}\nRebuild the candidate with \`sqt build --select ${selected.join(' ')}\`, ` +
		'then promote that build by the ID it prints.'
	);
}
