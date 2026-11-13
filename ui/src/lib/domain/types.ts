/**
 * The single authoritative SQLTide domain model.
 *
 * `main` (the SQLBuild Hub) re-declared mock shapes inline in six route files and
 * they diverged — node status was 'fresh'|'stale'|'warn'|'fail'|'source' in one
 * place and 'ok'|'fail'|'warn'|'stale'|'src' in another, needing a mapping layer.
 * Everything here is declared once and derived from there.
 *
 * Names mirror SQLTide's own vocabulary so this file stays a faithful mirror
 * of `target/manifest.json` + `sqltide_dag.json` + the direct plan payload.
 * See .ai/sqltide_ui_plan.md §2.
 */

// ─── replay ──────────────────────────────────────────────────────────────────

/** Boundary modes. `landed_at` is managed-source only; `cursor` is adopted only. */
type ReplayBoundaryMode = 'offsets' | 'timestamp' | 'landed_at' | 'cursor';

/** The five normalized replay column roles. */
export type ReplayRole = 'partition' | 'offset' | 'timestamp' | 'landed_at' | 'cursor';

/**
 * Why a model is or isn't a replay anchor. A model can anchor when it preserves
 * its source's required lineage, is non-aggregate, has no mutable side
 * reference, and doesn't set `replay_anchor never`.
 */
export type AnchorState = 'eligible' | 'aggregate' | 'mutable_ref' | 'never' | 'lineage_loss' | 'view';

// ─── sources ─────────────────────────────────────────────────────────────────

/** `kafka` = SQLTide owns the landing objects. `stream_table` = adopted, never mutated. */
type SourceKind = 'kafka' | 'stream_table' | 'postgres';

export type ManagedRelationKind = 'kafka_engine' | 'landing_mv' | 'landing_table';

type ManagedRelation = {
	kind: ManagedRelationKind;
	name: string;
	/** Rendered DDL for this managed object, straight from the compiler. */
	ddl: string | null;
};

export type PartitionState = {
	partition: number;
	/** Highest offset present in the SQLTide landing table. */
	offset: number | null;
	committedOffset: number | null;
	endOffset: number | null;
	kafkaLagMessages: number | null;
	newestEventAt: string;
};

/** A live Kafka table consuming as a different consumer group than the project declares. */
type ConsumerGroupDrift = {
	tableName: string;
	liveGroup: string;
	declaredGroup: string;
	message: string;
};

type SourceLiveState = {
	rowsPerSecond: number;
	/**
	 * Server-evaluated against the source's configured freshness policy
	 * (warn_after / error_after). null when no policy is configured.
	 */
	freshness: 'fresh' | 'lagging' | 'stalled' | null;
	/** Age of the newest landed row. This is not Kafka consumer lag. */
	lastArrivalSeconds: number | null;
	/** Broker high-water offset minus the consumer group's committed offset. */
	kafkaLagMessages: number | null;
	newestEventAt: string;
	/** Start of the retained extent — the floor of what any rebuild can reconstruct. */
	oldestEventAt: string;
	rows: number;
	partitions: PartitionState[];
	/** Recent throughput buckets, newest last. Drives the sparkline. */
	throughput: number[];
	/** The window the throughput buckets cover, from the server query. */
	throughputWindowSeconds: number | null;
	/** Set when the live Kafka table's consumer group differs from the declared one. */
	consumerGroupDrift: ConsumerGroupDrift | null;
};

export type Source = {
	name: string;
	kind: SourceKind;
	boundaryMode: ReplayBoundaryMode;
	/** The relation models actually read: `raw__<name>` when managed, `table_name` when adopted. */
	relationName: string;
	/** Empty for adopted sources — SQLTide creates nothing. */
	managedRelations: ManagedRelation[];
	/** Refresh cadence for scheduled sources the warehouse pulls. null = event driven. */
	refresh: string | null;
	/** ClickHouse TTL expression on the landing table. null = retained forever. */
	ttl: string | null;
	/** Parsed from `ttl` for arithmetic. null = infinite retention = lossless rebuilds. */
	retentionDays: number | null;
	brokerList: string | null;
	topic: string | null;
	consumerGroup: string | null;
	format: string | null;
	settings: Record<string, string> | null;
	/** Adopted sources only: declared role → user column. */
	columnMapping: Partial<Record<ReplayRole, string>> | null;
	live: SourceLiveState;
};

// ─── models ──────────────────────────────────────────────────────────────────

/** `table` creates a table + the MV that writes it. `view` is a terminal query view. */
type ModelKind = 'table' | 'view';

/** Typed graph edges. Exactly one `driving_input` per table model. */
export type RefType = 'driving_input' | 'reference' | 'mutable_reference';

export type ModelRef = {
	/** Logical name — a source name or another model name. */
	name: string;
	type: RefType;
	alias: string | null;
	/** True when the ref resolves to a source rather than a model. */
	isSource: boolean;
};

export type Column = {
	name: string;
	type: string;
	/** Set when this column carries a normalized replay role. */
	replayRole: ReplayRole | null;
	description: string | null;
};

type StorageSpec = {
	/** null for terminal views — storage fields are rejected on views. */
	engine: string | null;
	orderBy: string[];
	partitionBy: string | null;
	ttl: string | null;
	settings: Record<string, string> | null;
};

/**
 * How SQLTide regards a live relation. `unmanaged` and `conflicted` mean a
 * build would touch something it doesn't own — the Plan page makes both loud.
 */
type OwnershipState =
	| 'direct'
	| 'unmanaged'
	| 'conflicted'
	| 'absent'
	| 'virtual_environment';

/**
 * Freshness relative to the source, plus `drift` for code/warehouse mismatch.
 * `unknown` means no freshness policy is configured; `source` is used for
 * source nodes on the graph.
 */
export type ModelStatus = 'fresh' | 'lagging' | 'stalled' | 'drift' | 'unknown' | 'source';

type ModelActivity = {
	state: 'moving' | 'idle' | 'stalled' | 'unknown';
	source: 'query_views_log' | 'part_log' | 'system_parts' | 'unavailable';
	sourceAvailable: boolean;
	approximate: boolean;
	lastTriggeredAt: string | null;
	lastWriteAt: string | null;
	rowsWritten: number;
	windowSeconds: number;
	detail: string;
};

type SqlArtifacts = {
	/** The authored .sql file including its MODEL() header. */
	authored: string;
	/** Resolved SELECT after macro expansion and ref resolution. */
	compiled: string;
	tableDdl: string | null;
	mvDdl: string | null;
	viewDdl: string | null;
};

type DriftStatus = 'in_sync' | 'drift' | 'unavailable';

type ModelSemanticDrift = {
	comparison: 'live_vs_current_compiled';
	status: DriftStatus;
	message: string;
	liveDdl: string | null;
	schema: {
		status: DriftStatus;
		changes: { column: string; live: string | null; compiled: string | null; change: string }[];
	};
	query: {
		status: DriftStatus;
		relationName: string;
		live: string | null;
		compiled: string | null;
		unifiedDiff: string | null;
	};
	physicalConfiguration: {
		status: DriftStatus;
		changes: { field: string; live: string; compiled: string; status: DriftStatus }[];
	};
};

type ModelLiveState = {
	rows: number;
	diskBytes: number;
	parts: number;
	newestRowAt: string | null;
	/** Start of the extent this table actually holds — may predate source retention. */
	oldestRowAt: string | null;
	lagSeconds: number | null;
	/** Evaluated freshness, or null when no authored policy applies. */
	freshness?: 'fresh' | 'lagging' | 'stalled' | null;
	activity: ModelActivity;
	/** Live DDL matches compiled DDL. False = drift. */
	inSyncWithCompiled: boolean;
	/** Server-reported reasons the live warehouse diverges from compiled state. */
	driftReasons: string[];
	ownership: OwnershipState;
	/** From `sqltide_target_ownership.replay_coverage_json`. */
	recordedCoverage: { from: string; to: string } | null;
	semanticDrift: ModelSemanticDrift;
};

export type Model = {
	name: string;
	pipeline: string;
	kind: ModelKind;
	description: string | null;
	/** `tbl__<name>` / `view__<name>`, or an explicit `relation_name`. */
	relationName: string;
	/** `mv__<name>`. null for terminal views. */
	mvRelationName: string | null;
	/** Logical name of the single untyped driving input. null for terminal views. */
	drivingInput: string | null;
	refs: ModelRef[];
	columns: Column[];
	storage: StorageSpec;
	anchor: AnchorState;
	isAggregate: boolean;
	/** Set when MODEL() declares `replay_anchor never`. */
	anchorNever: boolean;
	sql: SqlArtifacts;
	live: ModelLiveState;
	status: ModelStatus;
};

// ─── pipelines ───────────────────────────────────────────────────────────────

type PipelineMode = 'direct' | 'virtual';

export type Pipeline = {
	name: string;
	/** Per-pipeline build mode from `pipeline.toml [mode]`, else the project default. */
	mode: PipelineMode;
	/** null for a valid view-only pipeline. */
	sourceName: string | null;
	/** Inherited from the source. null when source-less. */
	boundaryMode: ReplayBoundaryMode | null;
	/** Model names, in no particular order — read the tree instead. */
	models: string[];
	/** From an optional `pipeline.toml [naming]`. */
	naming: { tablePrefix: string | null; viewPrefix: string | null } | null;
	/** Present when `pipeline.toml` declares `[protection]`. */
	protection: { warning: string; confirmation: string } | null;
	auditDefaults: {
		severity: Severity | null;
		cadenceSeconds: number | null;
		warmupSeconds: number | null;
	};
	directory: string;
};

// ─── checks ──────────────────────────────────────────────────────────────────

type Severity = 'error' | 'warning';

export type CellValue = string | number | null;

export type QualityDriftReason =
	| 'binding_changed'
	| 'definition_changed'
	| 'execution_changed'
	| 'schedule_changed';

type QualityIdentity = {
	bindingKey: string;
	definitionFingerprint: string;
	executionFingerprint: string;
};

type AuditPolicy = {
	cadenceSeconds: number | null;
	warmupSeconds: number;
	scheduled: boolean;
};

type AuditResult = {
	passed: boolean;
	failingRowCount: number;
	sampleColumns: string[];
	sampleRows: CellValue[][];
	checkedAt: string;
	driftReasons: QualityDriftReason[];
	deferredUntil?: string | null;
};

export type Audit = {
	name: string;
	file: string;
	severity: Severity;
	description: string | null;
	referencedModels: string[];
	/** True when instantiated from `audits/generic/` via a schema.yml entry. */
	generic: boolean;
	/** The generic definition it came from, when `generic`. */
	genericName: string | null;
	sql: string;
	identity: QualityIdentity;
	policy: AuditPolicy;
	result: AuditResult | null;
};

/** Results are a two-sided bag diff, which renders as expected-vs-actual. */
type SqlTestTargetResult = {
	targetModelName: string;
	passed: boolean;
	columns: string[];
	missingRows: CellValue[][];
	unexpectedRows: CellValue[][];
};

type SqlTestResult = {
	passed: boolean;
	/** One diff per compared target — multi-target tests produce several. */
	targets: SqlTestTargetResult[];
	checkedAt: string;
	errorMessage: string | null;
	driftReasons: QualityDriftReason[];
};

export type SqlTest = {
	name: string;
	file: string;
	/** Multi-target tests exist — they land under `tests/_chain_/`. */
	targets: string[];
	sql: string;
	identity: QualityIdentity;
	result: SqlTestResult | null;
};

// ─── macros ──────────────────────────────────────────────────────────────────

type Macro = {
	name: string;
	file: string;
	signature: string;
	description: string | null;
};

// ─── project ─────────────────────────────────────────────────────────────────

export type Project = {
	name: string;
	target: string;
	database: string;
	adapter: string;
	timeZone: string;
	connection: { host: string; port: number; username: string; secure: boolean };
	vars: Record<string, string | number | boolean>;
	naming: { tablePrefix: string; viewPrefix: string };
	defaults: {
		managedSourceTtl: string | null;
		modelTtl: string | null;
		kafkaBrokerList: string | null;
		audits: {
			severity: Severity | null;
			cadenceSeconds: number | null;
			warmupSeconds: number | null;
		};
	};
	/** Server clock at snapshot time — everything relative is computed from this. */
	capturedAt: string;
	warehouseHealth: import('$lib/warehouse-health/types').WarehouseHealth | null;
	sources: Source[];
	pipelines: Pipeline[];
	models: Model[];
	audits: Audit[];
	tests: SqlTest[];
	macros: Macro[];
};

// ─── reconstruction (derived) ────────────────────────────────────────────────

/**
 * Compares what a model table holds against what its source can still
 * reconstruct. `truncating` is a standing latent condition: the next rebuild
 * silently drops the overhang. The CLI never tells you this.
 */
type ReconstructionState = 'matched' | 'truncating' | 'lossless' | 'unknown';

export type ReconstructionCoverage = {
	modelName: string;
	sourceName: string;
	state: ReconstructionState;
	heldFrom: string | null;
	retainedFrom: string | null;
	heldDays: number | null;
	retainedDays: number | null;
	/** Days of model history the source can no longer rebuild. */
	unreconstructableDays: number | null;
};

// ─── deployments ─────────────────────────────────────────────────────────────

export type DeploymentState =
	| 'active'
	| 'staged'
	| 'superseded'
	| 'incomplete'
	| 'metadata_missing'
	| 'physical_missing';

export type Deployment = {
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

type DeploymentModel = {
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

type DeploymentPromotionPreview = {
	classification: 'initial_publish' | 'promotion';
	additions: {
		database: string;
		logicalName: string;
		physicalName: string;
	}[];
	replacements: {
		database: string;
		logicalName: string;
		fromPhysicalName: string;
		toPhysicalName: string;
	}[];
	removals: {
		database: string;
		logicalName: string;
		physicalName: string;
	}[];
	blockedReason: string | null;
};

export type DeploymentDetail = Deployment & {
	database: string;
	models: DeploymentModel[];
	promotionPreview: DeploymentPromotionPreview | null;
	wouldOrphan: { relationNames: string[]; relationCount: number; bytes: number };
};
