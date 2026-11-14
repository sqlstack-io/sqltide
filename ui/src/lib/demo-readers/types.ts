/**
 * Runs one SQL statement against the warehouse and returns its rows decoded from ClickHouse
 * `JSONEachRow`, so 64-bit integers arrive as strings.
 */
export type Query = <Row>(sql: string) => Promise<Row[]>;

export type JsonObject = { [key: string]: unknown };

export type DeploymentState =
	| 'active'
	| 'staged'
	| 'superseded'
	| 'incomplete'
	| 'metadata_missing'
	| 'physical_missing';

export type DeploymentSummaryPayload = {
	deploymentId: string;
	state: DeploymentState;
	createdAt: string | null;
	publishedAt: string | null;
	persistedStatus: string | null;
	rootNames: string[];
	physicalRelationNames: string[];
	activeBindingNames: string[];
	missingRelationNames: string[];
	modelCount: number;
	relationCount: number;
	rows: number;
	bytes: number;
};

export type DeploymentsPayload = {
	database: string;
	deployments: DeploymentSummaryPayload[];
};

export type DeploymentModelPayload = {
	logicalName: string;
	stagedRelation: string;
	stagedRows: number;
	stagedBytes: number;
	liveRelation: string | null;
	liveDeploymentId: string | null;
	liveRows: number | null;
	isActive: boolean;
	isNew: boolean;
};

export type PromotionPreviewPayload = {
	classification: 'initial_publish' | 'promotion';
	additions: { database: string; logicalName: string; physicalName: string }[];
	replacements: {
		database: string;
		logicalName: string;
		fromPhysicalName: string;
		toPhysicalName: string;
	}[];
	removals: { database: string; logicalName: string; physicalName: string }[];
	blockedReason: string | null;
};

export type DeploymentDetailPayload = DeploymentSummaryPayload & {
	database: string;
	models: DeploymentModelPayload[];
	promotionPreview: PromotionPreviewPayload | null;
	wouldOrphan: { relationNames: string[]; relationCount: number; bytes: number };
};

export type DeploymentDiffStatus = 'added' | 'removed' | 'changed' | 'unchanged' | 'physical_missing';

export type DeploymentDiffRelationPayload = {
	logicalName: string;
	status: DeploymentDiffStatus;
	fromPhysicalName: string | null;
	toPhysicalName: string | null;
	fromRowCount: number | null;
	toRowCount: number | null;
	addedColumns: string[];
	removedColumns: string[];
};

export type DeploymentDiffPayload = {
	database: string;
	fromEndpoint: string;
	toEndpoint: string;
	relations: DeploymentDiffRelationPayload[];
};

/** One run event: its row columns merged with its JSON payload. */
export type RunEvent = JsonObject & {
	sequence: number;
	emittedAt: string;
	event: string;
	stepId: unknown;
	phase: unknown;
};

export type RunPayload = JsonObject & { invocationId: string; startedAt: string };

export type RunEventsPayload = {
	found: boolean;
	events: RunEvent[];
	hasMore: boolean;
	status: unknown;
	lastSignalAt: unknown;
	lastSignalAgeSeconds: unknown;
	statementProgress: JsonObject | null;
};

export type RunStatementPayload =
	| { found: false }
	| {
			found: true;
			invocationId: string;
			statementSequence: number;
			stepId: string;
			phase: string;
			intent: string;
			sql: string;
			sqlSha256: string;
			workflowSha256: string;
	  };

/** The parts of the `/api/definitions` payload the state overlay needs. */
export type StateDefinitions = {
	sources: { name: string; freshness?: { warnAfter: string | null; errorAfter: string | null } | null }[];
	pipelines: { sourceName: string | null; models: string[] }[];
	models: { name: string; kind: string }[];
};

/**
 * The `/api/state` payload. Model and source entries keep the fields the compiled project defines
 * and the readers replace every field the warehouse measures.
 */
export type StatePayload = {
	capturedAt: string;
	warehouseHealth: JsonObject | null;
	models: { [name: string]: JsonObject };
	sources: { [name: string]: JsonObject };
};
