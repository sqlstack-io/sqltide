/** The warehouse and metadata evidence the deployment readers reconstruct. */
import type { DeploymentState } from '../types.ts';

/** The logical key of one persisted deployment object. */
export type MetadataKey = {
	database: string | null;
	objectType: string;
	name: string;
};

export type PreparedMapping = {
	key: MetadataKey;
	physicalName: string;
	logicalModelName: string;
};

/** One persisted deployment, as the ClickHouse lifecycle inventory reconstructs it. */
export type DeploymentRecord = {
	deploymentId: string;
	createdAt: string;
	status: string;
	selectedRootKeys: MetadataKey[];
	mappings: PreparedMapping[];
};

export type Binding = {
	database: string;
	logicalName: string;
	physicalName: string;
};

export type PublishEvent = {
	publicationId: string;
	deploymentId: string;
	publishedAt: string;
	logicalViewNames: string[];
	bindings: Binding[];
};

export type DeploymentInventory = {
	deployments: DeploymentRecord[];
	publishEvents: PublishEvent[];
};

export type PhysicalCandidate = {
	database: string;
	logicalName: string;
	physicalName: string;
	objectType: 'table' | 'view';
};

export type ManagedTableState = {
	activeBindings: Binding[];
	physicalCandidates: PhysicalCandidate[];
};

export type CatalogColumn = {
	name: string;
	type: string;
	defaultExpression: string | null;
};

/** One relation of `system.tables` with the facts the readers derive from its SQL. */
export type CatalogRelation = {
	name: string;
	engine: string;
	definitionSql: string;
	columns: CatalogColumn[];
	sourceRelationNames: string[];
	targetRelationName: string | null;
	stableBindingName: string | null;
};

export type Catalog = {
	database: string;
	relations: CatalogRelation[];
};

export type DeploymentSummary = {
	deploymentId: string;
	state: DeploymentState;
	createdAt: string | null;
	persistedStatus: string | null;
	rootNames: string[];
	physicalRelationNames: string[];
	missingPhysicalRelationNames: string[];
	activeBindingNames: string[];
	latestPublishedAt: string | null;
};

export type Storage = { rows: number; bytes: number };

/** The bindings a promotion writes and the stale stable names it drops. */
export type BindingRequest = {
	bindings: Binding[];
	removals: { database: string; logicalName: string }[];
};

export type PromotionPreview = {
	initialPublish: boolean;
	additions: Binding[];
	replacements: { binding: Binding; fromPhysicalName: string }[];
	removals: { database: string; logicalName: string; physicalName: string }[];
	orphaned: { database: string; physicalName: string }[];
};

/** Everything one deployment read gathers from the warehouse. */
export type DeploymentEvidence = {
	database: string;
	inventory: DeploymentInventory;
	inspected: ManagedTableState;
	catalog: Catalog;
	summaries: DeploymentSummary[];
};
