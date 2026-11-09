/**
 * Everything the UI needs that isn't hand-authored in `mock.ts`.
 *
 * Keeping derivation here is what stops the six-divergent-mocks problem: the
 * physical graph, the stream tree, the rebuild closure, the plan and the
 * reconstruction analysis are all computed from one fixture.
 */

import {
	type Audit,
	type Deployment,
	type Model,
	type Pipeline,
	type Project,
	type ReconstructionCoverage,
	type Source,
	type SqlTest
} from '$lib/domain/types';
import { daysBetween } from '$lib/formatting/main/days-between';
import { formatEngineFamily } from '$lib/formatting/main/format-engine-family';
import type { Graph, GraphEdge, GraphNode } from '$lib/lineage/types';
import type { Plan, Selector } from '$lib/planning/types';
import type { WarehouseHealth } from '$lib/warehouse-health/types';

type PlanEntryReason = Plan['entries'][number]['reason'];
type ReconstructionState = ReconstructionCoverage['state'];
type IngestHealthState = 'healthy' | 'behind' | 'error' | 'partial' | 'no_kafka';

// ─── lookups ─────────────────────────────────────────────────────────────────

function modelByName(project: Project, name: string): Model | undefined {
	return project.models.find((model) => model.name === name);
}

function sourceByName(project: Project, name: string): Source | undefined {
	return project.sources.find((source) => source.name === name);
}

function pipelineByName(project: Project, name: string): Pipeline | undefined {
	return project.pipelines.find((pipeline) => pipeline.name === name);
}

function modelsInPipeline(project: Project, pipelineName: string): Model[] {
	return project.models.filter((model) => model.pipeline === pipelineName);
}

// ─── checks ──────────────────────────────────────────────────────────────────

function auditsForModel(project: Project, modelName: string): Audit[] {
	return project.audits.filter((audit) => audit.referencedModels.includes(modelName));
}

function testsForModel(project: Project, modelName: string): SqlTest[] {
	return project.tests.filter((test) => test.targets.includes(modelName));
}

type CheckCounts = {
	total: number;
	passing: number;
	warning: number;
	failing: number;
	executed: number;
	notRun: number;
};

function auditCounts(audits: Audit[]): CheckCounts {
	let passing = 0;
	let warning = 0;
	let failing = 0;
	for (const audit of audits) {
		if (!audit.result) continue;
		if (audit.result.deferredUntil) continue;
		if (audit.result.passed) passing += 1;
		else if (audit.severity === 'warning') warning += 1;
		else failing += 1;
	}
	const executed: number = passing + warning + failing;
	return { total: audits.length, passing, warning, failing, executed, notRun: audits.length - executed };
}

function testCounts(tests: SqlTest[]): CheckCounts {
	let passing = 0;
	let failing = 0;
	for (const test of tests) {
		if (!test.result) continue;
		if (test.result.passed) passing += 1;
		else failing += 1;
	}
	const executed: number = passing + failing;
	return { total: tests.length, passing, warning: 0, failing, executed, notRun: tests.length - executed };
}

// ─── graph: logical ──────────────────────────────────────────────────────────



/**
 * The measured flow state of a model's driving edge.
 *
 * Deliberately NOT derived from lag. Lag measures how far BEHIND the output is;
 * it says nothing about whether it is moving. A model ten minutes behind but
 * catching up at full throughput is flowing, arguably harder than one sitting
 * at two seconds. Treating lag as a proxy for motion made every slow-cadence
 * input — a cursor-polled dimension feed, say — look stopped when it was
 * merely behind.
 *
 * Missing lineage telemetry is unknown, not stalled: the edge remains visibly
 * driving but does not claim motion through animation.
 */
function modelFlowState(model: Model): GraphEdge['flowState'] {
	if (model.kind !== 'table') return 'unknown';
	if (model.live.activity.state === 'moving') return 'flowing';
	if (model.live.activity.state === 'stalled') return 'stalled';
	return 'unknown';
}

function sourceGraphNode(project: Project, source: Source): GraphNode {
	return {
		id: `source:${source.name}`,
		label: source.name,
		logicalName: source.name,
		logicalType: 'source',
		physicalType: null,
		status: 'source',
		anchor: null,
		kindLabel: source.kind === 'kafka' ? 'KAFKA SOURCE' : 'ADOPTED SOURCE',
		sublabel: source.kind === 'kafka' ? source.topic : source.relationName,
		rows: source.live.rows,
		rowsPerSecond: source.live.rowsPerSecond,
		failingChecks: 0,
		warningChecks: 0,
		totalChecks: 0,
		drift: false
	};
}

function modelGraphNode(project: Project, model: Model): GraphNode {
	const counts: CheckCounts = auditCounts(auditsForModel(project, model.name));
	return {
		id: `model:${model.name}`,
		label: model.name,
		logicalName: model.name,
		logicalType: model.kind === 'view' ? 'view' : 'model',
		physicalType: null,
		status: model.status,
		anchor: model.anchor,
		kindLabel:
			model.kind === 'view'
				? 'TERMINAL VIEW'
				: formatEngineFamily(model.storage.engine),
		sublabel: model.relationName,
		rows: model.live.rows,
		rowsPerSecond: null,
		failingChecks: counts.failing,
		warningChecks: counts.warning,
		totalChecks: counts.total,
		drift: !model.live.inSyncWithCompiled
	};
}

function buildLogicalGraph(project: Project): Graph {
	const nodes: GraphNode[] = [
		...project.sources.map((source) => sourceGraphNode(project, source)),
		...project.models.map((model) => modelGraphNode(project, model))
	];

	const edges: GraphEdge[] = [];
	for (const model of project.models) {
		for (const ref of model.refs) {
			const fromId: string = ref.isSource ? `source:${ref.name}` : `model:${ref.name}`;
			edges.push({
				id: `${fromId}->model:${model.name}:${ref.type}`,
				source: fromId,
				target: `model:${model.name}`,
				type: ref.type,
				flowState: ref.type === 'driving_input' ? modelFlowState(model) : 'unknown'
			});
		}
	}

	return { nodes, edges };
}

/**
 * Deployment-suffixed relations are real warehouse objects that the definition
 * graph cannot know about: a logical `tbl__x` may be backed by one active
 * relation while several superseded ones still occupy disk. Emitting them here
 * is what makes the physical view answer "what is actually in my database and
 * why".
 */
function appendDeploymentRelations(
	project: Project,
	deployments: Deployment[],
	nodes: GraphNode[],
	edges: GraphEdge[]
): void {
	if (deployments.length === 0) return;

	const modelByRelation: Map<string, Model> = new Map(
		project.models.map((model) => [model.relationName, model])
	);

	for (const deployment of deployments) {
		for (const relationName of deployment.physicalRelationNames) {
			const logicalName: string = logicalRelationName(relationName);
			const model: Model | undefined = modelByRelation.get(logicalName);
			if (model === undefined) continue;

			const parentId = `rel:${logicalName}`;
			const nodeId = `rel:${relationName}`;
			const state: 'active' | 'staged' | 'orphaned' = relationDeploymentState(
				deployment,
				logicalName
			);

			nodes.push({
				id: nodeId,
				label: relationName,
				logicalName: model.name,
				logicalType: 'model',
				physicalType: 'model_table',
				status: model.status,
				anchor: null,
				kindLabel: state.toUpperCase(),
				sublabel: deployment.deploymentId,
				rows: null,
				rowsPerSecond: null,
				failingChecks: 0,
				warningChecks: 0,
				totalChecks: 0,
				drift: false,
				deployment: { deploymentId: deployment.deploymentId, state }
			});
			edges.push({
				id: `${parentId}->${nodeId}`,
				source: parentId,
				target: nodeId,
				type: 'reference',
				flowState: 'unknown'
			});
		}
	}
}

/** `tbl__orders__20260410T005500Z_cd34ef` → `tbl__orders`. */
function logicalRelationName(relationName: string): string {
	const parts: string[] = relationName.split('__');
	return parts.length > 2 ? parts.slice(0, -1).join('__') : relationName;
}

/**
 * Binding is per deployment, not global: after promoting a deployment that
 * covered only part of the graph, two deployments can each hold live bindings,
 * and the relations they no longer back are orphaned even though the deployment
 * itself is still active.
 */
function relationDeploymentState(
	deployment: Deployment,
	logicalName: string
): 'active' | 'staged' | 'orphaned' {
	if (deployment.activeBindingNames.includes(logicalName)) return 'active';
	if (deployment.state === 'staged') return 'staged';
	return 'orphaned';
}

// ─── graph: physical ─────────────────────────────────────────────────────────

/**
 * Expands every logical node into the real ClickHouse objects. This is the view
 * operators actually debug and it exists in no other tool:
 *
 *   kafka__x ─▶ mv__x ─▶ raw__x ─▶ mv__model ─▶ tbl__model
 */
function buildPhysicalGraph(project: Project, deployments: Deployment[] = []): Graph {
	const nodes: GraphNode[] = [];
	const edges: GraphEdge[] = [];

	/** Logical name → the relation downstream MVs should read from. */
	const readRelationId = new Map<string, string>();

	for (const source of project.sources) {
		// Resolved by KIND, never by array position: the server serializes managed
		// relations in realization order (kafka engine, landing table, landing MV),
		// which is not the mock-era ordering this code once assumed.
		const kafkaRelation: Source['managedRelations'][number] | undefined = source.managedRelations.find(
			(r) => r.kind === 'kafka_engine'
		);
		const mvRelation: Source['managedRelations'][number] | undefined = source.managedRelations.find(
			(r) => r.kind === 'landing_mv'
		);
		const rawRelation: Source['managedRelations'][number] | undefined = source.managedRelations.find(
			(r) => r.kind === 'landing_table'
		);
		if (source.kind === 'kafka' && kafkaRelation && mvRelation && rawRelation) {
			const kafkaId = `rel:${kafkaRelation.name}`;
			const mvId = `rel:${mvRelation.name}`;
			const rawId = `rel:${rawRelation.name}`;

			nodes.push({
				id: kafkaId,
				label: kafkaRelation.name,
				logicalName: source.name,
				logicalType: 'source',
				physicalType: 'kafka_engine',
				status: 'source',
				anchor: null,
				kindLabel: 'KAFKA ENGINE',
				sublabel: source.topic,
				rows: null,
				rowsPerSecond: source.live.rowsPerSecond,
				failingChecks: 0,
				warningChecks: 0,
				totalChecks: 0,
				drift: false
			});
			nodes.push({
				id: mvId,
				label: mvRelation.name,
				logicalName: source.name,
				logicalType: 'source',
				physicalType: 'landing_mv',
				status: 'source',
				anchor: null,
				kindLabel: 'LANDING MV',
				sublabel: null,
				rows: null,
				rowsPerSecond: source.live.rowsPerSecond,
				failingChecks: 0,
				warningChecks: 0,
				totalChecks: 0,
				drift: false
			});
			nodes.push({
				id: rawId,
				label: rawRelation.name,
				logicalName: source.name,
				logicalType: 'source',
				physicalType: 'landing_table',
				status: 'source',
				anchor: null,
				kindLabel: 'LANDING TABLE',
				sublabel: source.ttl ? `TTL ${source.retentionDays}d` : 'no TTL',
				rows: source.live.rows,
				rowsPerSecond: source.live.rowsPerSecond,
				failingChecks: 0,
				warningChecks: 0,
				totalChecks: 0,
				drift: false
			});

			edges.push({
				id: `${kafkaId}->${mvId}`,
				source: kafkaId,
				target: mvId,
				type: 'driving_input',
				flowState: 'flowing'
			});
			edges.push({
				id: `${mvId}->${rawId}`,
				source: mvId,
				target: rawId,
				type: 'driving_input',
				flowState: 'flowing'
			});
			readRelationId.set(source.name, rawId);
		} else {
			const adoptedId = `rel:${source.relationName}`;
			nodes.push({
				id: adoptedId,
				label: source.relationName,
				logicalName: source.name,
				logicalType: 'source',
				physicalType: 'adopted_table',
				status: 'source',
				anchor: null,
				kindLabel: 'ADOPTED TABLE',
				sublabel: 'not owned by SQLTide',
				rows: source.live.rows,
				rowsPerSecond: source.live.rowsPerSecond,
				failingChecks: 0,
				warningChecks: 0,
				totalChecks: 0,
				drift: false
			});
			readRelationId.set(source.name, adoptedId);
		}
	}

	for (const model of project.models) {
		const counts: CheckCounts = auditCounts(auditsForModel(project, model.name));
		const tableId = `rel:${model.relationName}`;

		if (model.kind === 'view') {
			nodes.push({
				id: tableId,
				label: model.relationName,
				logicalName: model.name,
				logicalType: 'view',
				physicalType: 'model_view',
				status: model.status,
				anchor: model.anchor,
				kindLabel: 'VIEW',
				sublabel: null,
				rows: null,
				rowsPerSecond: null,
				failingChecks: counts.failing,
				warningChecks: counts.warning,
				totalChecks: counts.total,
				drift: !model.live.inSyncWithCompiled
			});
			readRelationId.set(model.name, tableId);
			continue;
		}

		const mvId = `rel:${model.mvRelationName}`;
		nodes.push({
			id: mvId,
			label: model.mvRelationName ?? '',
			logicalName: model.name,
			logicalType: 'model',
			physicalType: 'model_mv',
			status: model.status,
			anchor: null,
			kindLabel: 'MODEL MV',
			sublabel: null,
			rows: null,
			rowsPerSecond: null,
			failingChecks: 0,
			warningChecks: 0,
			totalChecks: 0,
			drift: false
		});
		nodes.push({
			id: tableId,
			label: model.relationName,
			logicalName: model.name,
			logicalType: 'model',
			physicalType: 'model_table',
			status: model.status,
			anchor: model.anchor,
			kindLabel: formatEngineFamily(model.storage.engine),
			sublabel: null,
			rows: model.live.rows,
			rowsPerSecond: null,
			failingChecks: counts.failing,
			warningChecks: counts.warning,
			totalChecks: counts.total,
			drift: !model.live.inSyncWithCompiled
		});
		edges.push({
			id: `${mvId}->${tableId}`,
			source: mvId,
			target: tableId,
			type: 'driving_input',
			flowState: modelFlowState(model)
		});
		readRelationId.set(model.name, tableId);
	}

	// Wire each model's inputs into the MV that writes it (or into the view itself).
	for (const model of project.models) {
		const targetId: string =
			model.kind === 'view' ? `rel:${model.relationName}` : `rel:${model.mvRelationName}`;
		for (const ref of model.refs) {
			const fromId: string | undefined = readRelationId.get(ref.name);
			if (!fromId) continue;
			edges.push({
				id: `${fromId}->${targetId}:${ref.type}`,
				source: fromId,
				target: targetId,
				type: ref.type,
				flowState: ref.type === 'driving_input' ? modelFlowState(model) : 'unknown'
			});
		}
	}

	appendDeploymentRelations(project, deployments, nodes, edges);

	return { nodes, edges };
}

// ─── stream tree ─────────────────────────────────────────────────────────────

type StreamTreeRow = {
	kind: 'source' | 'model';
	name: string;
	depth: number;
	/** Vertical guide state for each ancestor level, for drawing tree connectors. */
	ancestorHasNext: boolean[];
	isLast: boolean;
	model: Model | null;
	source: Source | null;
};

/**
 * The driving-input graph inside a pipeline is a TREE, because every table model
 * has exactly one driving input. That is why a pipeline reads well as an
 * indented list and a dbt DAG never can.
 */
function streamTree(project: Project, pipelineName: string): StreamTreeRow[] {
	const pipeline: Pipeline | undefined = pipelineByName(project, pipelineName);
	if (!pipeline) return [];

	const models: Model[] = modelsInPipeline(project, pipelineName);
	const childrenOf = new Map<string, Model[]>();
	const roots: Model[] = [];

	for (const model of models) {
		const parent: string | null = model.drivingInput;
		const parentIsModelInPipeline: boolean =
			parent !== null && models.some((candidate) => candidate.name === parent);
		if (parentIsModelInPipeline && parent) {
			const bucket: Model[] = childrenOf.get(parent) ?? [];
			bucket.push(model);
			childrenOf.set(parent, bucket);
		} else {
			roots.push(model);
		}
	}

	const rows: StreamTreeRow[] = [];

	const source: Source | undefined = pipeline.sourceName
		? sourceByName(project, pipeline.sourceName)
		: undefined;
	if (source) {
		rows.push({
			kind: 'source',
			name: source.name,
			depth: 0,
			ancestorHasNext: [],
			isLast: false,
			model: null,
			source
		});
	}

	const baseDepth: number = source ? 1 : 0;

	function walk(model: Model, depth: number, ancestorHasNext: boolean[], isLast: boolean): void {
		rows.push({
			kind: 'model',
			name: model.name,
			depth,
			ancestorHasNext,
			isLast,
			model,
			source: null
		});
		const children: Model[] = childrenOf.get(model.name) ?? [];
		children.forEach((child, index) => {
			walk(child, depth + 1, [...ancestorHasNext, !isLast], index === children.length - 1);
		});
	}

	roots.forEach((root, index) => {
		walk(root, baseDepth, [], index === roots.length - 1);
	});

	return rows;
}

// ─── selection & closure ─────────────────────────────────────────────────────

function parseSelector(token: string): Selector | null {
	const trimmed: string = token.trim();
	if (!trimmed) return null;
	if (trimmed.startsWith('pipeline:')) {
		const name: string = trimmed.slice('pipeline:'.length);
		return name ? { kind: 'pipeline', name } : null;
	}
	if (trimmed.startsWith('model:')) {
		const name: string = trimmed.slice('model:'.length);
		return name ? { kind: 'model', name } : null;
	}
	return { kind: 'model', name: trimmed };
}

function selectorToken(selector: Selector): string {
	return selector.kind === 'pipeline' ? `pipeline:${selector.name}` : selector.name;
}

/** Downstream neighbours across every edge type. */
function downstreamMap(project: Project): Map<string, string[]> {
	const map = new Map<string, string[]>();
	for (const model of project.models) {
		for (const ref of model.refs) {
			if (ref.isSource) continue;
			const bucket: string[] = map.get(ref.name) ?? [];
			bucket.push(model.name);
			map.set(ref.name, bucket);
		}
	}
	return map;
}

type Closure = {
	/** Exactly what the selectors named. */
	selected: Set<string>;
	/** Selected plus every downstream model. Direct plans are never pruned. */
	all: Set<string>;
	reasonByModel: Map<string, PlanEntryReason>;
};

function resolveClosure(project: Project, selectors: Selector[]): Closure {
	const selected = new Set<string>();

	if (selectors.length === 0) {
		const all = new Set<string>(project.models.map((model) => model.name));
		const reasonByModel = new Map<string, PlanEntryReason>();
		for (const name of all) reasonByModel.set(name, 'all_models');
		return { selected: all, all, reasonByModel };
	}

	for (const selector of selectors) {
		// Names are globally unique across pipelines and models, so a bare token
		// resolves to whichever it is; a bare pipeline name needs no pipeline: prefix.
		if (selector.kind === 'pipeline') {
			for (const model of modelsInPipeline(project, selector.name)) selected.add(model.name);
		} else if (modelByName(project, selector.name)) {
			selected.add(selector.name);
		} else if (pipelineByName(project, selector.name)) {
			for (const model of modelsInPipeline(project, selector.name)) selected.add(model.name);
		}
	}

	const downstream: Map<string, string[]> = downstreamMap(project);
	const all = new Set<string>(selected);
	const stack: string[] = [...selected];
	while (stack.length) {
		const current: string = stack.pop() as string;
		for (const child of downstream.get(current) ?? []) {
			if (!all.has(child)) {
				all.add(child);
				stack.push(child);
			}
		}
	}

	const reasonByModel = new Map<string, PlanEntryReason>();
	for (const name of all) {
		reasonByModel.set(name, selected.has(name) ? 'selected' : 'downstream_of_selected');
	}

	return { selected, all, reasonByModel };
}

/** Dependency order (inputs first). Used for creation; reversed for teardown. */
function topologicalModelOrder(project: Project, names: Set<string>): string[] {
	const ordered: string[] = [];
	const visited = new Set<string>();

	function visit(name: string): void {
		if (visited.has(name)) return;
		visited.add(name);
		const model: Model | undefined = modelByName(project, name);
		if (model) {
			for (const ref of model.refs) {
				if (!ref.isSource && names.has(ref.name)) visit(ref.name);
			}
		}
		ordered.push(name);
	}

	for (const name of names) visit(name);
	return ordered;
}

// ─── reconstruction ──────────────────────────────────────────────────────────

/**
 * Model tables are disposable derivations of `raw__*`; the source TTL is the
 * durability setting. So a model holding MORE history than its source retains
 * is a standing latent truncation — the next rebuild silently drops the
 * overhang. The CLI never tells you this.
 */
function reconstructionCoverage(project: Project): ReconstructionCoverage[] {
	const rows: ReconstructionCoverage[] = [];

	for (const model of project.models) {
		if (model.kind === 'view') continue;
		const source: Source | undefined = rootSourceFor(project, model);
		if (!source || !model.live.oldestRowAt) {
			rows.push({
				modelName: model.name,
				sourceName: source?.name ?? '—',
				state: 'unknown',
				heldFrom: model.live.oldestRowAt,
				retainedFrom: source?.live.oldestEventAt ?? null,
				heldDays: null,
				retainedDays: null,
				unreconstructableDays: null
			});
			continue;
		}

		const reference: string = project.capturedAt;
		const heldDays: number = daysBetween(model.live.oldestRowAt, reference);
		const retainedDays: number = daysBetween(source.live.oldestEventAt, reference);

		let state: ReconstructionState;
		if (source.retentionDays === null) state = 'lossless';
		else if (heldDays - retainedDays > 1) state = 'truncating';
		else state = 'matched';

		rows.push({
			modelName: model.name,
			sourceName: source.name,
			state,
			heldFrom: model.live.oldestRowAt,
			retainedFrom: source.live.oldestEventAt,
			heldDays,
			retainedDays,
			unreconstructableDays: state === 'truncating' ? heldDays - retainedDays : 0
		});
	}

	return rows;
}

/** Walks driving inputs up to the source that roots this model's pipeline. */
function rootSourceFor(project: Project, model: Model): Source | undefined {
	let current: Model | undefined = model;
	const seen = new Set<string>();
	while (current && current.drivingInput && !seen.has(current.name)) {
		seen.add(current.name);
		const source: Source | undefined = sourceByName(project, current.drivingInput);
		if (source) return source;
		current = modelByName(project, current.drivingInput);
	}
	return undefined;
}

// ─── rollups ─────────────────────────────────────────────────────────────────

type FreshnessSummary = {
	fresh: number;
	lagging: number;
	stalled: number;
	drift: number;
	unknown: number;
	monitored: number;
	unmonitored: number;
	total: number;
	offenders: Model[];
};

function freshnessSummary(project: Project): FreshnessSummary {
	let fresh = 0;
	let lagging = 0;
	let stalled = 0;
	let drift = 0;
	let unknown = 0;
	const offenders: Model[] = [];

	for (const model of project.models) {
		const evaluatedFreshness: Model['live']['freshness'] = model.live.freshness;
		if (evaluatedFreshness === 'fresh') fresh += 1;
		if (evaluatedFreshness === 'lagging') {
			lagging += 1;
			offenders.push(model);
		}
		if (evaluatedFreshness === 'stalled') {
			stalled += 1;
			offenders.push(model);
		}
		if (model.status === 'drift') {
			drift += 1;
			if (!offenders.includes(model)) offenders.push(model);
		}
		if (evaluatedFreshness === null || evaluatedFreshness === undefined) unknown += 1;
	}

	const monitored: number = fresh + lagging + stalled;
	return {
		fresh,
		lagging,
		stalled,
		drift,
		unknown,
		monitored,
		unmonitored: unknown,
		total: project.models.length,
		offenders
	};
}

function driftedModels(project: Project): Model[] {
	return project.models.filter((model) => !model.live.inSyncWithCompiled);
}

function truncatingCoverage(project: Project): ReconstructionCoverage[] {
	return reconstructionCoverage(project).filter((row) => row.state === 'truncating');
}

function pipelineFreshness(project: Project, pipelineName: string): FreshnessSummary {
	const models: Model[] = modelsInPipeline(project, pipelineName);
	let fresh = 0;
	let lagging = 0;
	let stalled = 0;
	let drift = 0;
	let unknown = 0;
	const offenders: Model[] = [];
	for (const model of models) {
		const evaluatedFreshness: Model['live']['freshness'] = model.live.freshness;
		if (evaluatedFreshness === 'fresh') fresh += 1;
		if (evaluatedFreshness === 'lagging') lagging += 1;
		if (evaluatedFreshness === 'stalled') stalled += 1;
		if (model.status === 'drift') drift += 1;
		if (evaluatedFreshness === null || evaluatedFreshness === undefined) unknown += 1;
		if (evaluatedFreshness === 'lagging' || evaluatedFreshness === 'stalled' || model.status === 'drift') {
			offenders.push(model);
		}
	}
	const monitored: number = fresh + lagging + stalled;
	return {
		fresh,
		lagging,
		stalled,
		drift,
		unknown,
		monitored,
		unmonitored: unknown,
		total: models.length,
		offenders
	};
}

function ingestHealthSummary(project: Project) {
	const managedSources: Source[] = project.sources.filter((source) => source.kind === 'kafka');
	const consumers: WarehouseHealth['kafkaConsumers'] =
		project.warehouseHealth?.kafkaConsumers ?? null;
	if (managedSources.length === 0) {
		return {
			state: 'no_kafka' as const,
			configured: 0,
			materialized: 0,
			polling: 0,
			exceptions: 0,
			stuckConsumers: [],
			notBuilt: 0,
			behind: 0,
			lagUnavailable: 0
		};
	}
	if (consumers === null) {
		const behind: number = managedSources.filter(
			(source) => source.live.kafkaLagMessages !== null && source.live.kafkaLagMessages > 0
		).length;
		return {
			state: 'partial' as const,
			configured: managedSources.length,
			materialized: null,
			polling: null,
			exceptions: null,
			stuckConsumers: [],
			notBuilt: null,
			behind,
			lagUnavailable: null
		};
	}
	const notBuilt: number = Math.max(
		consumers.configuredTables - consumers.materializedTables,
		0
	);
	const materializedTableNames: Set<string> = new Set(consumers.materializedTableNames);
	const materializedSources: Source[] = managedSources.filter((source) =>
		source.managedRelations.some(
			(relation) => relation.kind === 'kafka_engine' && materializedTableNames.has(relation.name)
		)
	);
	const behind: number = materializedSources.filter(
		(source) => source.live.kafkaLagMessages !== null && source.live.kafkaLagMessages > 0
	).length;
	const lagUnavailable: number = materializedSources.filter(
		(source) => source.live.kafkaLagMessages === null
	).length;
	const state: IngestHealthState =
		consumers.exceptionTables > 0 ||
		consumers.stuckConsumers.length > 0 ||
		consumers.pollingTables < consumers.materializedTables
			? ('error' as const)
			: lagUnavailable > 0
				? ('partial' as const)
				: behind > 0
					? ('behind' as const)
					: ('healthy' as const);
	return {
		state,
		configured: consumers.configuredTables,
		materialized: consumers.materializedTables,
		polling: consumers.pollingTables,
		exceptions: consumers.exceptionTables,
		stuckConsumers: consumers.stuckConsumers,
		notBuilt,
		behind,
		lagUnavailable
	};
}

export function deadLetterSummary(project: Project) {
	const deadLetters: WarehouseHealth['deadLetters'] | undefined = project.warehouseHealth?.deadLetters;
	if (deadLetters === undefined) {
		return { state: 'none' as const, total: 0, models: [] };
	}
	if (deadLetters === null) {
		return { state: 'unavailable' as const, total: 0, models: [] };
	}
	const total: number = deadLetters.reduce((sum, model) => sum + model.rowCount, 0);
	return {
		state: total > 0 ? ('present' as const) : ('none' as const),
		total,
		models: deadLetters
	};
}

function projectHealthSummary(project: Project) {
	return {
		freshness: freshnessSummary(project),
		ingest: ingestHealthSummary(project),
		deadLetters: deadLetterSummary(project),
		drifted: driftedModels(project)
	};
}

function anchorCount(project: Project, pipelineName: string): number {
	return modelsInPipeline(project, pipelineName).filter((model) => model.anchor === 'eligible')
		.length;
}

/**
 * Every stream source that ultimately feeds a set of models — walking driving
 * inputs up through intermediate models rather than only looking at direct
 * `__source` refs. This is what bounds the replay window on the Plan page.
 */
function rootSourcesFor(project: Project, modelNames: Iterable<string>): Source[] {
	const found = new Map<string, Source>();
	for (const name of modelNames) {
		const model: Model | undefined = modelByName(project, name);
		if (!model) continue;
		const source: Source | undefined = rootSourceFor(project, model);
		if (source) found.set(source.name, source);
	}
	return [...found.values()];
}

export const domainDerivations = {
	modelByName,
	sourceByName,
	pipelineByName,
	modelsInPipeline,
	auditsForModel,
	testsForModel,
	auditCounts,
	testCounts,
	modelFlowState,
	buildLogicalGraph,
	buildPhysicalGraph,
	streamTree,
	parseSelector,
	selectorToken,
	resolveClosure,
	topologicalModelOrder,
	reconstructionCoverage,
	rootSourceFor,
	projectHealthSummary,
	truncatingCoverage,
	pipelineFreshness,
	anchorCount,
	rootSourcesFor
};
