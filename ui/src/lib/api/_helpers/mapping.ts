/**
 * Maps dev-server payloads onto the UI domain model.
 *
 * The server payloads were designed against these types, so most of this is
 * mechanical renaming plus the handful of derivations that are cheaper client
 * side (replay roles from column names and macro signatures).
 */

import type {
	AnchorState,
	Audit,
	CellValue,
	Column,
	Model,
	ModelStatus,
	PartitionState,
	Pipeline,
	Project,
	ReplayRole,
	Source,
	SqlTest
} from '$lib/domain/types';
import type { WarehouseHealth, WarehouseMemoryHealth } from '$lib/warehouse-health/types';
import type { CheckStatusRecord } from '$lib/api/types';
import type { Plan, PlanSqlChangeStatus } from '$lib/planning/types';
import { configureTimeZone } from '$lib/formatting/main/configure-time-zone';

type Payload = Record<string, unknown>;

const REPLAY_ROLE_BY_COLUMN: Record<string, ReplayRole> = {
	_replay_partition: 'partition',
	_replay_offset: 'offset',
	_replay_timestamp: 'timestamp',
	_replay_landed_at: 'landed_at',
	_replay_cursor: 'cursor'
};

export function projectFromServer(definitions: Payload, state: Payload): Project {
	const header: Payload = definitions.project as Payload;
	const connection: Record<string, unknown> = (header.connection ?? {}) as Record<string, unknown>;
	const naming: Payload = (header.naming ?? {}) as Payload;
	const defaults: Payload = (header.defaults ?? {}) as Payload;
	const ui: Payload = (header.ui ?? {}) as Payload;
	const auditDefaults: Payload = (defaults.audits ?? {}) as Payload;
	const models: Model[] = (definitions.models as Payload[]).map((model) =>
		modelFromServer(model, stateFor(state, 'models', model.name as string))
	);
	const timeZone: string = (ui.timezone as string) ?? 'UTC';
	configureTimeZone(timeZone);
	return {
		name: (header.name as string) ?? 'project',
		target: (header.target as string) ?? '',
		database: (header.database as string) ?? '',
		adapter: (header.adapter as string) ?? 'clickhouse',
		timeZone,
		connection: {
			host: String(connection.host ?? ''),
			port: Number(connection.port ?? 0),
			username: String(connection.username ?? ''),
			secure: Boolean(connection.secure ?? false)
		},
		vars: (header.vars ?? {}) as Project['vars'],
		naming: {
			tablePrefix: (naming.tablePrefix as string) ?? 'tbl__',
			viewPrefix: (naming.viewPrefix as string) ?? 'view__'
		},
		defaults: {
			managedSourceTtl: (defaults.managedSourceTtl as string | null) ?? null,
			modelTtl: (defaults.modelTtl as string | null) ?? null,
			kafkaBrokerList: (defaults.kafkaBrokerList as string | null) ?? null,
			audits: {
				severity: (auditDefaults.severity as Project['defaults']['audits']['severity']) ?? null,
				cadenceSeconds: (auditDefaults.cadenceSeconds as number | null) ?? null,
				warmupSeconds: (auditDefaults.warmupSeconds as number | null) ?? null
			}
		},
		capturedAt: (state.capturedAt as string) ?? new Date().toISOString(),
		warehouseHealth: warehouseHealthFromServer(state.warehouseHealth),
		sources: (definitions.sources as Payload[]).map((source) =>
			sourceFromServer(source, stateFor(state, 'sources', source.name as string))
		),
		pipelines: (definitions.pipelines as Payload[]).map(pipelineFromServer),
		models,
		audits: (definitions.audits as Payload[]).map(auditFromServer),
		tests: (definitions.tests as Payload[]).map(testFromServer),
		macros: (definitions.macros as Payload[]).map((macro) => ({
			name: macro.name as string,
			file: macro.file as string,
			signature: signatureFromSource(macro.source as string, macro.name as string),
			description: (macro.description as string | null) ?? null
		}))
	};
}

function warehouseHealthFromServer(value: unknown): WarehouseHealth | null {
	if (value === null || typeof value !== 'object') return null;
	const health: Payload = value as Payload;
	const inodes: Payload = (health.inodes ?? {}) as Payload;
	const memory: Payload | null = (health.memory ?? null) as Payload | null;
	const activity: Payload | null = (health.activity ?? null) as Payload | null;
	return {
		availability: health.availability as WarehouseHealth['availability'],
		status: health.status as WarehouseHealth['status'],
		adapter: String(health.adapter ?? ''),
		database: String(health.database ?? ''),
		version: (health.version as string | null) ?? null,
		uptimeSeconds: (health.uptimeSeconds as number | null) ?? null,
		measuredAt: String(health.measuredAt ?? ''),
		collectionDurationMs: Number(health.collectionDurationMs ?? 0),
		stale: Boolean(health.stale ?? false),
		warnings: (health.warnings as string[]) ?? [],
		capacityWarningFraction: nullableNumber(health.capacityWarningFraction),
		capacityCriticalFraction: nullableNumber(health.capacityCriticalFraction),
		disks: ((health.disks ?? []) as Payload[]).map((disk) => ({
			name: String(disk.name ?? ''),
			path: disk.path === null || disk.path === undefined ? null : String(disk.path),
			type: disk.type === null || disk.type === undefined ? null : String(disk.type),
			totalBytes: nullableNumber(disk.totalBytes),
			freeBytes: nullableNumber(disk.freeBytes),
			unreservedBytes: nullableNumber(disk.unreservedBytes),
			keepFreeBytes: nullableNumber(disk.keepFreeBytes),
			status: disk.status as WarehouseHealth['status']
		})),
		inodes: {
			total: (inodes.total as number | null) ?? null,
			free: (inodes.free as number | null) ?? null,
			status: (inodes.status as WarehouseHealth['status']) ?? 'unknown'
		},
		memory:
			memory === null
				? null
				: {
						residentBytes: (memory.residentBytes as number | null) ?? null,
						hostTotalBytes: (memory.hostTotalBytes as number | null) ?? null,
						cgroupUsedBytes: (memory.cgroupUsedBytes as number | null) ?? null,
						cgroupLimitBytes: (memory.cgroupLimitBytes as number | null) ?? null,
						basis: memory.basis as WarehouseMemoryHealth['basis'],
						pressureFraction: (memory.pressureFraction as number | null) ?? null
					},
		activity:
			activity === null
				? null
				: {
						activeQueries: nullableNumber(activity.activeQueries),
						activeMerges: nullableNumber(activity.activeMerges),
						incompleteMutations: nullableNumber(activity.incompleteMutations)
					},
		kafkaConsumers:
			health.kafkaConsumers === null || health.kafkaConsumers === undefined
				? null
				: {
						configuredTables: Number((health.kafkaConsumers as Payload).configuredTables ?? 0),
						materializedTables: Number((health.kafkaConsumers as Payload).materializedTables ?? 0),
						materializedTableNames: stringArray(
							(health.kafkaConsumers as Payload).materializedTableNames
						),
						pollingTables: Number((health.kafkaConsumers as Payload).pollingTables ?? 0),
						exceptionTables: Number((health.kafkaConsumers as Payload).exceptionTables ?? 0),
						stuckConsumers: (((health.kafkaConsumers as Payload).stuckConsumers ?? []) as Payload[]).map(
							(consumer) => ({
								table: String(consumer.table ?? ''),
								stuckSeconds: nullableNumber(consumer.stuckSeconds),
								recentErrors: Number(consumer.recentErrors ?? 0),
								latestError: String(consumer.latestError ?? '')
							})
						)
					},
		deadLetters:
			health.deadLetters === null || health.deadLetters === undefined
				? null
				: (health.deadLetters as Payload[]).map((model) => ({
						modelName: String(model.modelName ?? ''),
						rowCount: Number(model.rowCount ?? 0),
						latestFailedColumns: stringArray(model.latestFailedColumns),
						latestPayload: String(model.latestPayload ?? ''),
						latestQuarantinedAt: String(model.latestQuarantinedAt ?? '')
					})),
		tables:
			health.tables === null || health.tables === undefined
				? null
				: (health.tables as Payload[]).map((table) => ({
						name: String(table.name ?? ''),
						rows: nullableNumber(table.rows),
						bytesOnDisk: nullableNumber(table.bytesOnDisk),
						activeParts: nullableNumber(table.activeParts)
					}))
	};
}

function stringArray(value: unknown): string[] {
	return Array.isArray(value) ? value.map(String) : [];
}

function nullableNumber(value: unknown): number | null {
	return value === null || value === undefined ? null : Number(value);
}

function stateFor(state: Payload, section: string, name: string): Payload {
	const bucket: Record<string, Payload> = (state[section] ?? {}) as Record<string, Payload>;
	return bucket[name] ?? {};
}

function sourceFromServer(source: Payload, live: Payload): Source {
	const kafka: Payload | null = (source.kafka ?? null) as Payload | null;
	const throughput: Payload | null = (live.throughput ?? null) as Payload | null;
	const buckets: number[] = (throughput?.buckets ?? []) as number[];
	return {
		name: source.name as string,
		kind: source.kind as Source['kind'],
		refresh: (source.refresh ?? null) as string | null,
		boundaryMode: source.boundaryMode as Source['boundaryMode'],
		relationName: source.relationName as string,
		managedRelations: ((source.managedRelations ?? []) as Payload[]).map((relation) => ({
			kind: relation.kind as Source['managedRelations'][number]['kind'],
			name: relation.name as string,
			ddl: (relation.ddl as string | null) ?? null
		})),
		ttl: (source.ttl as string | null) ?? null,
		retentionDays: retentionDaysFromSource(source),
		brokerList: (kafka?.brokerList as string) ?? null,
		topic: (kafka?.topic as string) ?? null,
		consumerGroup: (kafka?.consumerGroup as string) ?? null,
		format: (kafka?.format as string) ?? null,
		settings: (kafka?.settings as Record<string, string> | null) ?? null,
		columnMapping: (source.columnMapping as Source['columnMapping']) ?? null,
		live: {
			rowsPerSecond: (live.rowsPerSecond as number) ?? 0,
			freshness: (live.freshness as Source['live']['freshness']) ?? null,
			lastArrivalSeconds: (live.lastArrivalSeconds as number | null) ?? null,
			kafkaLagMessages: (live.kafkaLagMessages as number | null) ?? null,
			newestEventAt: (live.newestEventAt as string) ?? '',
			oldestEventAt: (live.oldestEventAt as string) ?? '',
			rows: (live.rows as number) ?? 0,
			partitions: ((live.partitions ?? []) as Payload[]).map(partitionFromServer),
			throughput: buckets,
			throughputWindowSeconds: (throughput?.windowSeconds as number | null) ?? null
		}
	};
}

function partitionFromServer(partition: Payload): PartitionState {
	const newest: string = (partition.newestEventAt as string) ?? '';
	return {
		partition: partition.partition as number,
		offset: (partition.maxOffset as number | null) ?? null,
		committedOffset: (partition.committedOffset as number | null) ?? null,
		endOffset: (partition.endOffset as number | null) ?? null,
		kafkaLagMessages: (partition.kafkaLagMessages as number | null) ?? null,
		newestEventAt: newest
	};
}

function pipelineFromServer(pipeline: Payload): Pipeline {
	const naming: Payload = (pipeline.naming ?? {}) as Payload;
	const protection: Payload | null = (pipeline.protection ?? null) as Payload | null;
	const auditDefaults: Payload = (pipeline.auditDefaults ?? {}) as Payload;
	return {
		name: pipeline.name as string,
		mode: (pipeline.mode as Pipeline['mode']) ?? 'direct',
		sourceName: (pipeline.sourceName as string | null) ?? null,
		boundaryMode: (pipeline.boundaryMode as Pipeline['boundaryMode']) ?? null,
		models: (pipeline.models as string[]) ?? [],
		naming: {
			tablePrefix: (naming.tablePrefix as string | null) ?? null,
			viewPrefix: (naming.viewPrefix as string | null) ?? null
		},
		protection:
			protection === null
				? null
				: {
						warning: (protection.warning as string) ?? '',
						confirmation: (protection.confirmation as string) ?? ''
					},
		auditDefaults: {
			severity: (auditDefaults.severity as Pipeline['auditDefaults']['severity']) ?? null,
			cadenceSeconds: (auditDefaults.cadenceSeconds as number | null) ?? null,
			warmupSeconds: (auditDefaults.warmupSeconds as number | null) ?? null
		},
		directory: (pipeline.directory as string) ?? ''
	};
}

function modelFromServer(model: Payload, live: Payload): Model {
	const sql: Payload = (model.sql ?? {}) as Payload;
	const ddl: Payload = (sql.ddl ?? {}) as Payload;
	const storage: Payload | null = (model.storage ?? null) as Payload | null;
	const anchor: AnchorState = model.anchor as AnchorState;
	const freshness: string | null = (live.freshness as string | null) ?? null;
	const drift: boolean = Boolean(live.drift ?? false);
	const activity: Payload = (live.activity ?? {}) as Payload;
	const semanticDrift: Payload = (live.semanticDrift ?? {}) as Payload;
	const schemaDrift: Payload = (semanticDrift.schema ?? {}) as Payload;
	const queryDrift: Payload = (semanticDrift.query ?? {}) as Payload;
	const physicalDrift: Payload = (semanticDrift.physicalConfiguration ?? {}) as Payload;
	return {
		name: model.name as string,
		pipeline: model.pipeline as string,
		kind: model.kind === 'view' ? 'view' : 'table',
		description: (model.description as string | null) ?? null,
		relationName: model.relationName as string,
		mvRelationName: (model.mvRelationName as string | null) ?? null,
		drivingInput: (model.drivingInput as string | null) ?? null,
		refs: ((model.refs ?? []) as Payload[]).map((ref) => ({
			name: ref.name as string,
			type: ref.type as Model['refs'][number]['type'],
			alias: null,
			isSource: Boolean(ref.isSource)
		})),
		columns: ((model.columns ?? []) as Payload[]).map(columnFromServer),
		storage: {
			engine: (storage?.engine as string) ?? null,
			orderBy: (storage?.orderBy as string[]) ?? [],
			partitionBy: (storage?.partitionBy as string | null) ?? null,
			ttl: (storage?.ttl as string | null) ?? null,
			settings: (storage?.settings as Record<string, string> | null) ?? null
		},
		anchor,
		isAggregate: Boolean(model.isAggregate),
		anchorNever: anchor === 'never',
		sql: {
			authored: (sql.authored as string) ?? '',
			compiled: (sql.compiled as string) ?? '',
			tableDdl: (ddl.table as string | null) ?? null,
			mvDdl: (ddl.materializedView as string | null) ?? null,
			viewDdl: (ddl.view as string | null) ?? null
		},
		live: {
			rows: (live.rows as number) ?? 0,
			diskBytes: (live.diskBytes as number) ?? 0,
			parts: (live.parts as number) ?? 0,
			newestRowAt: (live.newestRowAt as string | null) ?? null,
			oldestRowAt: (live.oldestRowAt as string | null) ?? null,
			lagSeconds: (live.lagSeconds as number | null) ?? null,
			freshness: freshness as Model['live']['freshness'],
			activity: {
				state: (activity.state as Model['live']['activity']['state']) ?? 'unknown',
				source: (activity.source as Model['live']['activity']['source']) ?? 'unavailable',
				sourceAvailable: Boolean(activity.sourceAvailable ?? false),
				approximate: Boolean(activity.approximate ?? false),
				lastTriggeredAt: (activity.lastTriggeredAt as string | null) ?? null,
				lastWriteAt: (activity.lastWriteAt as string | null) ?? null,
				rowsWritten: (activity.rowsWritten as number) ?? 0,
				windowSeconds: (activity.windowSeconds as number) ?? 0,
				detail: (activity.detail as string) ?? 'ClickHouse activity telemetry is unavailable.'
			},
			inSyncWithCompiled: !drift,
			driftReasons: (live.driftReasons as string[]) ?? [],
			ownership: ((live.ownership as string) ?? 'absent') as Model['live']['ownership'],
			recordedCoverage: (live.recordedCoverage as Model['live']['recordedCoverage']) ?? null,
			semanticDrift: {
				comparison: 'live_vs_current_compiled',
				status: (semanticDrift.status as Model['live']['semanticDrift']['status']) ?? 'unavailable',
				message:
					(semanticDrift.message as string) ?? 'No live comparison evidence is available.',
				liveDdl: (semanticDrift.liveDdl as string | null) ?? null,
				schema: {
					status: (schemaDrift.status as Model['live']['semanticDrift']['schema']['status']) ?? 'unavailable',
					changes:
						(schemaDrift.changes as Model['live']['semanticDrift']['schema']['changes']) ?? []
				},
				query: {
					status: (queryDrift.status as Model['live']['semanticDrift']['query']['status']) ?? 'unavailable',
					relationName: (queryDrift.relationName as string) ?? model.relationName,
					live: (queryDrift.live as string | null) ?? null,
					compiled: (queryDrift.compiled as string | null) ?? null,
					unifiedDiff: (queryDrift.unifiedDiff as string | null) ?? null
				},
				physicalConfiguration: {
					status:
						(physicalDrift.status as Model['live']['semanticDrift']['physicalConfiguration']['status']) ??
						'unavailable',
					changes:
						(physicalDrift.changes as Model['live']['semanticDrift']['physicalConfiguration']['changes']) ??
						[]
				}
			}
		},
		status: statusFromState(freshness, drift)
	};
}

function statusFromState(freshness: string | null, drift: boolean): ModelStatus {
	if (drift) return 'drift';
	if (freshness === 'lagging' || freshness === 'stalled') return freshness;
	return freshness === 'fresh' ? 'fresh' : 'unknown';
}

function columnFromServer(column: Payload): Column {
	const name: string = column.name as string;
	return {
		name,
		type: column.type as string,
		replayRole: REPLAY_ROLE_BY_COLUMN[name] ?? null,
		description: (column.description as string | null) ?? null
	};
}

function auditFromServer(audit: Payload): Audit {
	const genericName: string | null = (audit.genericName as string | null) ?? null;
	const identity: Payload = audit.identity as Payload;
	const policy: Payload = audit.policy as Payload;
	return {
		name: audit.name as string,
		file: (audit.file as string) ?? '',
		severity: (audit.severity as Audit['severity']) ?? 'error',
		description: (audit.description as string | null) ?? null,
		referencedModels: (audit.referencedModels as string[]) ?? [],
		generic: genericName !== null,
		genericName,
		sql: (audit.sql as string) ?? '',
		identity: {
			bindingKey: identity.bindingKey as string,
			definitionFingerprint: identity.definitionFingerprint as string,
			executionFingerprint: identity.executionFingerprint as string
		},
		policy: {
			cadenceSeconds: (policy.cadenceSeconds as number | null) ?? null,
			warmupSeconds: Number(policy.warmupSeconds ?? 0),
			scheduled: Boolean(policy.scheduled)
		},
		result: null
	};
}

function testFromServer(test: Payload): SqlTest {
	const identity: Payload = test.identity as Payload;
	return {
		name: test.name as string,
		file: (test.file as string) ?? '',
		targets: (test.targets as string[]) ?? [],
		sql: (test.sql as string) ?? '',
		identity: {
			bindingKey: identity.bindingKey as string,
			definitionFingerprint: identity.definitionFingerprint as string,
			executionFingerprint: identity.executionFingerprint as string
		},
		result: null
	};
}

export function planFromServer(payload: Payload, adapter: string): Plan {
	const mode: Plan['mode'] =
		payload.mode === 'virtual' ? 'virtual' : payload.mode === 'mixed' ? 'mixed' : 'direct';
	const upperBoundary: Payload = (payload.upperBoundary as Payload | undefined) ?? {};
	const common: Omit<Plan, 'mode' | 'deploymentId'> = {
		adapter,
		database: (payload.database as string) ?? '',
		userScope: ((payload.userScope as string[]) ?? []).map(parseUserScope),
		entries: ((payload.entries as Payload[]) ?? []).map((entry) => ({
			modelName: entry.modelName as string,
			pipeline: (entry.pipeline as string) ?? '',
			reason: entry.reason as Plan['entries'][number]['reason'],
			relationNames: (entry.relationNames as string[]) ?? [],
			resourceKinds: (entry.resourceKinds as Plan['entries'][number]['resourceKinds']) ?? [],
			ownership: (entry.ownership as Plan['entries'][number]['ownership']) ?? [],
			drivingInput: (entry.drivingInput as string | null) ?? null,
			isReplayRoot: Boolean(entry.isReplayRoot),
			sqlChange: sqlChangeFromServer((entry.sqlChange as Payload | null) ?? null)
		})),
		prerequisites: ((payload.prerequisites as Payload[]) ?? []).map((item) => ({
			name: item.name as string,
			type: item.type === 'source' ? ('source' as const) : ('model' as const),
			relationNames: (item.relationNames as string[]) ?? [],
			present: Boolean(item.present),
			frameworkManaged: Boolean(item.frameworkManaged)
		})),
		teardown: ((payload.teardown as Payload[]) ?? []).map(operationFromServer),
		creation: ((payload.creation as Payload[]) ?? []).map(operationFromServer),
		replayRoots: ((payload.replayRoots as Payload[]) ?? []).map((root) => ({
			modelName: root.modelName as string,
			drivingInputName: (root.drivingInputName as string) ?? '',
			drivingInputRelationName: (root.drivingInputRelationName as string) ?? '',
			boundaryMode: root.boundaryMode as Plan['replayRoots'][number]['boundaryMode'],
			replayColumns: (root.replayColumns as Plan['replayRoots'][number]['replayColumns']) ?? {},
			propagatedModelNames: (root.propagatedModelNames as string[]) ?? [],
			hasAggregateSemantics: Boolean(root.hasAggregateSemantics),
			rowsToReplay: (root.rowsToReplay as number | null) ?? null,
			settings: (root.settings as Record<string, string>) ?? {}
		})),
		warnings: ((payload.warnings as Payload[]) ?? []).map((warning) => ({
			code: (warning.code as string) ?? '',
			message: (warning.message as string) ?? '',
			relatedModel: (warning.relatedModel as string | null) ?? null
		})),
		sourceRecreations: ((payload.sourceRecreations as Payload[]) ?? []).map((recreation) => ({
			sourceName: (recreation.sourceName as string) ?? '',
			relationName: (recreation.relationName as string) ?? '',
			landingViewName: (recreation.landingViewName as string) ?? '',
			landingRelationName: (recreation.landingRelationName as string) ?? '',
			stagedRelationName: (recreation.stagedRelationName as string) ?? '',
			consumerGroup: (recreation.consumerGroup as string) ?? '',
			driftedSettings: (recreation.driftedSettings as string[]) ?? [],
			message: (recreation.message as string) ?? '',
			selectedPipeline: (recreation.selectedPipeline as string | null) ?? null,
			otherReaderPipelines: (recreation.otherReaderPipelines as string[]) ?? []
		})),
		protections: ((payload.protections as Payload[]) ?? []).map((protection) => ({
			pipelineName: (protection.pipelineName as string) ?? '',
			warning: (protection.warning as string) ?? '',
			confirmation: (protection.confirmation as string) ?? ''
		})),
		replayWindow: (payload.replayWindow as Plan['replayWindow']) ?? { mode: 'full' },
		plannedAt: (payload.plannedAt as string) ?? '',
		command: (payload.command as string) ?? 'sqt build',
		executionOrder: ((payload.executionOrder as string[]) ?? []).filter(
			(item): item is Plan['executionOrder'][number] => item === 'direct' || item === 'virtual'
		),
		phases: ((payload.phases as Payload[]) ?? []).map(planPhaseFromServer),
		upperBoundary: {
			mode: 'captured_at_execution' as const,
			continuesLive: Boolean(upperBoundary.continuesLive)
		}
	};
	if (mode === 'direct') return { ...common, mode, deploymentId: null };
	return { ...common, mode, deploymentId: (payload.deploymentId as string) ?? '' };
}

function planPhaseFromServer(phase: Payload): Plan['phases'][number] {
	return {
		mode: phase.mode === 'virtual' ? 'virtual' : 'direct',
		effect: phase.effect === 'staged' ? 'staged' : 'applied_immediately',
		deploymentId: (phase.deploymentId as string | null) ?? null,
		modelNames: (phase.modelNames as string[]) ?? [],
		contextModelNames: (phase.contextModelNames as string[]) ?? [],
		relationNames: (phase.relationNames as string[]) ?? [],
		actions: ((phase.actions as Payload[]) ?? []).map((action) => ({
			phase: (action.phase as string) ?? '',
			action: (action.action as string) ?? '',
			logicalName: (action.logicalName as string) ?? '',
			physicalName: (action.physicalName as string | null) ?? null
		})),
		startTime: (phase.startTime as string | null) ?? null
	};
}

function sqlChangeFromServer(
	item: Payload | null
): Plan['entries'][number]['sqlChange'] {
	if (item === null) return null;
	return {
		status: item.status as PlanSqlChangeStatus,
		unifiedDiff: (item.unifiedDiff as string | null) ?? null,
		warning: (item.warning as string | null) ?? null
	};
}

function operationFromServer(item: Payload): Plan['teardown'][number] {
	return {
		relationName: item.relationName as string,
		action: item.action === 'create' ? 'create' : 'drop',
		modelName: (item.modelName as string) ?? '',
		resourceKind: item.resourceKind as Plan['teardown'][number]['resourceKind']
	};
}

function parseUserScope(token: string): Plan['userScope'][number] {
	return token.startsWith('pipeline:')
		? { kind: 'pipeline', name: token.slice('pipeline:'.length) }
		: { kind: 'model', name: token };
}

function signatureFromSource(source: string, name: string): string {
	const match: RegExpMatchArray | null = source.match(new RegExp(`def\\s+${name}\\s*\\(([^)]*)\\)`));
	return match ? `${name}(${match[1]})` : `${name}(…)`;
}

function retentionDaysFromTtl(ttl: string | null): number | null {
	if (ttl === null) return null;
	const match: RegExpMatchArray | null = ttl.match(/INTERVAL\s+(\d+)\s+DAY/i);
	return match ? Number(match[1]) : null;
}

function retentionDaysFromSource(source: Payload): number | null {
	const retention: Payload | null = (source.retention ?? null) as Payload | null;
	const durationSeconds: number | null = nullableNumber(retention?.durationSeconds);
	return durationSeconds === null
		? retentionDaysFromTtl((source.ttl as string | null) ?? null)
		: durationSeconds / 86_400;
}

/**
 * Fold recorded outcomes from `_sqltide_node_results` into the project.
 * CLI runs and UI runs share that history, so a refresh no longer forgets what
 * passed. Never overwrites a FRESHER in-session run.
 */
export function applyRecordedCheckStatuses(
	project: Project,
	statuses: CheckStatusRecord[]
): void {
	for (const record of statuses) {
		if (record.status === 'never_run' || record.completedAt === null) continue;
		if (record.kind === 'audit') applyAuditStatus(project, record);
		else applyTestStatus(project, record);
	}
}

function recordedIso(completedAt: string): string {
	return `${completedAt.replace(' ', 'T')}Z`;
}

function recordedPassed(record: CheckStatusRecord): boolean {
	if (record.status === 'passed') return true;
	return record.driftReasons.length > 0 && record.failureCount === 0 && record.errorMessage === null;
}

function isNewerThanExisting(checkedAt: string | undefined, record: CheckStatusRecord): boolean {
	if (checkedAt === undefined) return true;
	return Date.parse(recordedIso(record.completedAt ?? '')) >= Date.parse(checkedAt);
}

function applyAuditStatus(project: Project, record: CheckStatusRecord): void {
	const audit: Audit | undefined = project.audits.find((item) => item.name === record.name);
	if (!audit || !isNewerThanExisting(audit.result?.checkedAt, record)) return;
	const payload: Record<string, unknown> = record.payload ?? {};
	audit.result = {
		passed: recordedPassed(record),
		failingRowCount: record.failureCount,
		sampleColumns: (payload.sample_column_names as string[]) ?? [],
		sampleRows: (payload.sample_rows as CellValue[][]) ?? [],
		checkedAt: recordedIso(record.completedAt ?? ''),
		driftReasons: record.driftReasons,
		deferredUntil: (payload.eligible_at as string | null) ?? null
	};
}

function applyTestStatus(project: Project, record: CheckStatusRecord): void {
	const test: SqlTest | undefined = project.tests.find((item) => item.name === record.name);
	if (!test || !isNewerThanExisting(test.result?.checkedAt, record)) return;
	const payload: Record<string, unknown> = record.payload ?? {};
	const targets: NonNullable<SqlTest['result']>['targets'] = ((payload.targets as Payload[]) ?? []).map((target) => ({
		targetModelName: String(target.target_model_name ?? target.name ?? ''),
		passed: Boolean(
			target.passed ??
				(((target.missing_rows as unknown[]) ?? []).length === 0 &&
					((target.unexpected_rows as unknown[]) ?? []).length === 0)
		),
		columns: (target.columns as string[]) ?? [],
		missingRows: (target.missing_rows as CellValue[][]) ?? [],
		unexpectedRows: (target.unexpected_rows as CellValue[][]) ?? []
	}));
	test.result = {
		passed: recordedPassed(record),
		targets,
		checkedAt: recordedIso(record.completedAt ?? ''),
		errorMessage: record.errorMessage,
		driftReasons: record.driftReasons
	};
}
