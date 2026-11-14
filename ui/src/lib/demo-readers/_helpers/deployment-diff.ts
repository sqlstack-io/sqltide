/** Which relations a deployment diff compares and how each comparison is summarized. */
import { DEPLOYMENT_STATUS_INCOMPLETE, PUBLISHED_OBJECT_TYPES } from '../constants.ts';
import type { DeploymentDiffStatus } from '../types.ts';
import { compareText } from './values.ts';
import type { Binding, CatalogColumn, DeploymentRecord } from './evidence-types.ts';

/** The endpoint naming the current stable bindings. */
const ACTIVE_DIFF_ENDPOINT: string = 'active';

export type RelationPair = {
	database: string;
	logicalName: string;
	fromPhysicalName: string | null;
	toPhysicalName: string | null;
};

/** Whether the endpoint binds the relation, and its columns and row count when it exists. */
export type DiffSide = { bound: boolean; found: { columns: CatalogColumn[]; rowCount: number } | null };

export function identifierError(value: string): string | null {
	return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? null : `Deployment diff cannot query invalid identifier '${value}'`;
}

/** `(from, to)` endpoints of `DEPLOYMENT` (compared with the active bindings) or `FROM:TO`. */
export function parseComparison(comparison: string): [string, string] {
	const parts: string[] = comparison.split(':');
	const [from, to]: [string, string] =
		parts.length === 1 && parts[0] !== ''
			? [ACTIVE_DIFF_ENDPOINT, parts[0]]
			: parts.length === 2 && parts[0] !== '' && parts[1] !== ''
				? [parts[0], parts[1]]
				: (() => {
						throw new Error('deployment diff expects DEPLOYMENT or FROM:TO');
					})();
	if (from === to) throw new Error('deployment diff endpoints must be different');
	return [from, to];
}

function key(database: string, name: string): string {
	return `${database}\u0000${name}`;
}

function endpointRelations(
	endpoint: string,
	deployments: DeploymentRecord[],
	active: Binding[],
	database: string
): Map<string, string> {
	if (endpoint === ACTIVE_DIFF_ENDPOINT) {
		if (active.length === 0) throw new Error("deployment diff endpoint 'active' has no stable bindings");
		return new Map(active.map((binding) => [key(binding.database, binding.logicalName), binding.physicalName]));
	}
	const deployment: DeploymentRecord | undefined = deployments.find((item) => item.deploymentId === endpoint);
	if (deployment === undefined) throw new Error(`Unknown deployment diff endpoint '${endpoint}'`);
	if (deployment.status === DEPLOYMENT_STATUS_INCOMPLETE) {
		throw new Error(`Deployment diff endpoint '${endpoint}' is incomplete`);
	}
	const relations: Map<string, string> = new Map(
		deployment.mappings
			.filter((mapping) => PUBLISHED_OBJECT_TYPES.includes(mapping.key.objectType))
			.map((mapping) => [key(mapping.key.database || database, mapping.key.name), mapping.physicalName])
	);
	if (relations.size === 0) throw new Error(`Deployment diff endpoint '${endpoint}' has no model relations`);
	return relations;
}

/** Every logical relation either endpoint binds, sorted by `(database, logical name)`. */
export function relationPairs(
	from: string,
	to: string,
	deployments: DeploymentRecord[],
	active: Binding[],
	database: string
): RelationPair[] {
	const fromRelations: Map<string, string> = endpointRelations(from, deployments, active, database);
	const toRelations: Map<string, string> = endpointRelations(to, deployments, active, database);
	const names: string[] = [...new Set([...fromRelations.keys(), ...toRelations.keys()])].sort(compareText);
	return names.map((name) => {
		const [pairDatabase, logicalName] = name.split('\u0000');
		return {
			database: pairDatabase,
			logicalName,
			fromPhysicalName: fromRelations.get(name) ?? null,
			toPhysicalName: toRelations.get(name) ?? null
		};
	});
}

function sameFound(left: DiffSide['found'], right: DiffSide['found']): boolean {
	if (left === null || right === null) return left === right;
	return left.rowCount === right.rowCount && JSON.stringify(left.columns) === JSON.stringify(right.columns);
}

/** The summary of one relation comparison. */
export function diffStatus(from: DiffSide, to: DiffSide): DeploymentDiffStatus {
	if ((from.bound && from.found === null) || (to.bound && to.found === null)) return 'physical_missing';
	if (!from.bound) return 'added';
	if (!to.bound) return 'removed';
	return sameFound(from.found, to.found) ? 'unchanged' : 'changed';
}

/** Names of `left` columns absent from `right`, in `left` order. */
export function columnNamesMissingFrom(left: CatalogColumn[], right: CatalogColumn[]): string[] {
	const rightNames: Set<string> = new Set(right.map((column) => column.name));
	return left.map((column) => column.name).filter((name) => !rightNames.has(name));
}
