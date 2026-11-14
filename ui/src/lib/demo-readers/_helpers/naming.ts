/** Deployment identifiers and the deployment-suffixed physical names built from them. */
import { MATERIALIZED_VIEW_NAME_PREFIX } from '../constants.ts';

const DEPLOYMENT_SEPARATOR: string = '__';
const KAFKA_TABLE_NAME_PREFIX: string = 'kafka__';

const DEPLOYMENT_ID: RegExp = /^\p{Nd}{8}T\p{Nd}{6}Z_[A-Za-z0-9]+$/u;

export function isDeploymentId(text: string): boolean {
	return DEPLOYMENT_ID.test(text);
}

/** `(logical name, deployment ID)` split at the last `__`, if there is one. */
export function splitPhysicalName(physicalName: string): [string, string] | null {
	const at: number = physicalName.lastIndexOf(DEPLOYMENT_SEPARATOR);
	return at < 0 ? null : [physicalName.slice(0, at), physicalName.slice(at + DEPLOYMENT_SEPARATOR.length)];
}

export function isDeploymentPhysicalName(physicalName: string): boolean {
	const parts: [string, string] | null = splitPhysicalName(physicalName);
	return parts !== null && parts[0] !== '' && isDeploymentId(parts[1]);
}

/** The deployment of a deployment physical name, else `null`. */
export function deploymentIdOf(physicalName: string): string | null {
	return isDeploymentPhysicalName(physicalName) ? (splitPhysicalName(physicalName)?.[1] ?? null) : null;
}

/** The logical name before the last separator, or the whole name without one. */
export function logicalNameOf(physicalName: string): string {
	return splitPhysicalName(physicalName)?.[0] ?? physicalName;
}

/** The logical name of a deployment-suffixed model candidate; Kafka tables and writers are not. */
export function candidateLogicalName(physicalName: string): string | null {
	if (!isDeploymentPhysicalName(physicalName)) return null;
	const logical: string = logicalNameOf(physicalName);
	return logical.startsWith(KAFKA_TABLE_NAME_PREFIX) || logical.startsWith(MATERIALIZED_VIEW_NAME_PREFIX)
		? null
		: logical;
}

/** Deployment relations that receive a stable view; materialized-view writers are excluded. */
export function modelRelationNames(relationNames: string[]): string[] {
	return relationNames.filter((name) => !logicalNameOf(name).startsWith(MATERIALIZED_VIEW_NAME_PREFIX));
}
