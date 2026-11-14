import type { Deployment, Project } from '$lib/domain/types';
import type { AuthConfig, AuthPayload, Capabilities } from '$lib/auth/types';

export type BuildStartResult = {
	invocationId: string;
	command: string;
	status: string;
};

export type RunEvent = {
	sequence: number;
	emittedAt: string;
	event: string;
	stepId: string | null;
	displayName?: string | null;
	phase: string | null;
	command?: string;
	mode?: string;
	displayCommand?: string;
	selectors?: string[];
	startTime?: string | null;
	totalStatements?: number;
	selectedNodeCount?: number;
	executedLogicalIds?: string[];
	contextLogicalIds?: string[];
	startupTimings?: {
		compileMs: number;
		observabilityMs: number;
		planningMs: number;
		totalMs: number;
	} | null;
	statementSequence?: number;
	queryId?: string | null;
	warehouseTerminationConfirmed?: boolean | null;
	intent?: string;
	elapsedMs?: number;
	writtenRows?: number | null;
	errorMessage?: string | null;
	status?: string;
	failureCount?: number;
	deferredUntil?: string | null;
	outcome?: string;
	exitCode?: number;
};

export type RunStatus =
	| 'succeeded'
	| 'failed'
	| 'cancelled'
	| 'cancelling'
	| 'cancellation_failed'
	| 'running'
	| 'unresponsive'
	| 'presumed_failed';

export type RunEventFeed = {
	found: boolean;
	events: RunEvent[];
	hasMore: boolean;
	status: RunStatus | null;
	lastSignalAt: string | null;
	lastSignalAgeSeconds: number | null;
	statementProgress: {
		found: boolean;
		queryId: string;
		statementSequence: number;
		stepId: string | null;
		phase: string | null;
		observedAt: string;
		elapsedSeconds?: number;
		readRows?: number;
		readBytes?: number;
		totalRowsApprox?: number;
		memoryUsageBytes?: number;
		readRowsPerSecond?: number;
		readBytesPerSecond?: number;
		settings?: Record<string, string>;
		replayOffsetProgress?: {
			percentage: number;
			etaSeconds: number | null;
			completedSpan: number;
			totalSpan: number;
			observedPartitions: number;
			totalPartitions: number;
		};
	} | null;
};

export type RunStatement = {
	found: boolean;
	invocationId?: string;
	statementSequence?: number;
	stepId?: string;
	phase?: string;
	intent?: string;
	sql?: string;
	sqlSha256?: string;
	workflowSha256?: string;
};

export type BuildFeed = {
	running: boolean;
	invocationId: string | null;
	currentInvocationId: string | null;
	command: string;
	exitCode: number | null;
	events: RunEvent[];
	stderr: string;
	forceAvailable: boolean;
	activeQueryId?: string | null;
	cancellationStatus?: RunStatus | null;
	cancellationError?: string | null;
};

export type RunRecord = {
	invocationId: string;
	command: string;
	displayCommand: string | null;
	mode: string;
	status: RunStatus;
	outcome: RunStatus;
	exitCode: number | null;
	startedAt: string;
	completedAt: string | null;
	lastSignalAt: string;
	lastSignalAgeSeconds: number;
	durationMs: number;
	selectedNodeCount: number;
	errorMessage: string | null;
	toolVersion: string;
	lastActivity: string | null;
	completedOperationCount: number | null;
	totalStatements: number | null;
	currentStep: string | null;
	auditSummary: {
		passed: number;
		warning: number;
		failed: number;
		error: number;
		total: number;
	} | null;
};

export type QualityDriftReason =
	| 'binding_changed'
	| 'definition_changed'
	| 'execution_changed'
	| 'schedule_changed';

export type CheckStatusRecord = {
	kind: 'audit' | 'test';
	name: string;
	status:
		| 'passed'
		| 'warning'
		| 'failed'
		| 'error'
		| 'deferred'
		| 'binding_changed'
		| 'definition_changed'
		| 'execution_changed'
		| 'schedule_changed'
		| 'never_run';
	driftReasons: QualityDriftReason[];
	severity: string | null;
	failureCount: number;
	completedAt: string | null;
	payload: Record<string, unknown> | null;
	errorMessage: string | null;
	missingRelations: string[];
};

export type CheckRunResult = {
	passed: boolean;
	deferredUntil?: string | null;
	missingRelations?: string[];
	failingRowCount?: number;
	sampleColumns?: string[];
	sampleRows?: (string | number | null)[][];
	errorMessage?: string | null;
	targets?: {
		targetModelName: string;
		passed: boolean;
		columns?: string[];
		missingRows: (string | number | null)[][];
		unexpectedRows: (string | number | null)[][];
	}[];
};

export type DeploymentDiffRelation = {
	logicalName: string;
	status: 'added' | 'removed' | 'changed' | 'unchanged' | 'physical_missing';
	fromPhysicalName: string | null;
	toPhysicalName: string | null;
	fromRowCount: number | null;
	toRowCount: number | null;
	addedColumns: string[];
	removedColumns: string[];
};

export type DeploymentDiff = {
	database: string;
	fromEndpoint: string;
	toEndpoint: string;
	relations: DeploymentDiffRelation[];
};

export type PromoteResult = {
	invocationId: string;
	deploymentId: string;
	publishedViews: { logicalName: string; physicalName: string }[];
	graphAtomicPublish: boolean;
	/** The deployment a rollback replaced; promotions do not report it. */
	previousDeploymentId?: string;
};

export type CleanupResult = {
	invocationId: string;
	removedRelations: number;
	removedDeployments: number;
};

export type CompileError = {
	message: string;
	path: string | null;
	line: number | null;
	column: number | null;
};

export type ServerStatus = {
	state: 'ok' | 'failing';
	toolVersion: string;
	versionKey: string;
	compiledAt: string;
	timings: Record<string, number> | null;
	error: CompileError | null;
	warehouseConnected: boolean;
	warehouseError: string | null;
	warehouseState: string;
	warehouseLastAttemptAt: string | null;
	warehouseNextAttemptAt: string | null;
};

export type AppPhase = 'loading' | 'ready' | 'compile_failing' | 'unreachable';

export type AppState = {
	phase: AppPhase;
	status: ServerStatus | null;
	project: Project | null;
	deployments: Deployment[];
	reloading: boolean;
	fetchError: string | null;
};

export type AppController = {
	readonly app: AppState;
	initialize(): Promise<void>;
	initializeFromBootstrap(payload: BootstrapPayload): void;
	reload(): Promise<void>;
	refreshLiveState(options?: { force?: boolean }): Promise<void>;
	refreshDeployments(): Promise<void>;
};

export type BootstrapPayload = {
	auth: {
		config: AuthConfig;
		session: AuthPayload;
		capabilities: Capabilities | null;
	};
	status: Record<string, unknown>;
	definitions: Record<string, unknown> | null;
	state: Record<string, unknown> | null;
};
