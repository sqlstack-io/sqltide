import type { RunEvent } from '$lib/api/types';
import { buildLogicalGraph } from '$lib/domain/main/graphs/build-logical-graph';
import type { Project } from '$lib/domain/types';
import { formatCompact } from '$lib/formatting/main/format-compact';
import type { Graph, GraphNode } from '$lib/lineage/types';
import type {
	RunGraphInput,
	RunGraphPresentation,
	RunNodeNote
} from '$lib/run-presentation/types';

type ModelRunState = { state: 'running' | 'done' | 'failed'; rows: number | null };
type RunGraphOperation = 'build' | 'promotion' | 'destruction';

export function buildRunGraph(input: RunGraphInput): RunGraphPresentation {
	const modelStates: Map<string, ModelRunState> = buildModelStates(input.project, input.events);
	const fullGraph: Graph = buildLogicalGraph(input.project);
	const executedIds: Set<string> = new Set(input.startedEvent?.executedLogicalIds ?? []);
	const contextIds: Set<string> = new Set(input.startedEvent?.contextLogicalIds ?? []);
	const runGraph: Graph = {
		nodes: fullGraph.nodes.filter(
			(node: GraphNode) => executedIds.has(node.id) || contextIds.has(node.id)
		),
		edges: fullGraph.edges.filter(
			(edge) =>
				(executedIds.has(edge.source) || contextIds.has(edge.source)) &&
				(executedIds.has(edge.target) || contextIds.has(edge.target))
		)
	};
	const recordedScopeCount: number = executedIds.size + contextIds.size;
	const mutedIds: Set<string> = new Set(
		runGraph.nodes.filter((node: GraphNode) => contextIds.has(node.id)).map((node: GraphNode) => node.id)
	);
	const command: string = input.startedEvent?.command ?? input.record?.command ?? input.commandLine;
	const operation: RunGraphOperation =
		command === 'deployment promote' || command === 'deployment rollback'
			? 'promotion'
			: command === 'destroy pipelines' || command === 'reset target'
				? 'destruction'
				: 'build';
	return {
		runGraph,
		mutedIds,
		notes: buildNotes(runGraph, modelStates, input.running, input.outcome, operation, executedIds),
		recordedScopeCount,
		missingScopeCount: recordedScopeCount - runGraph.nodes.length
	};
}

function buildModelStates(project: Project, events: RunEvent[]): Map<string, ModelRunState> {
	const states: Map<string, ModelRunState> = new Map();
	for (const event of events) {
		const model: string | null = modelForStep(project, event.stepId);
		if (model === null) continue;
		if (event.event === 'statement_started' && !states.has(model)) {
			states.set(model, { state: 'running', rows: null });
		}
		if (event.event !== 'statement_completed') continue;
		if (event.errorMessage) states.set(model, { state: 'failed', rows: null });
		else if ((event.stepId ?? '').startsWith('replay_')) {
			states.set(model, { state: 'done', rows: event.writtenRows ?? null });
		} else if ((event.stepId ?? '').startsWith('replace_stable_binding_')) {
			states.set(model, { state: 'done', rows: null });
		} else if (states.get(model)?.state !== 'done') {
			states.set(model, { state: 'running', rows: null });
		}
	}
	return states;
}

function modelForStep(project: Project, stepId: string | null): string | null {
	if (stepId === null) return null;
	if (stepId.startsWith('replay_')) {
		const name: string = stepId.slice('replay_'.length);
		return project.models.some((model: Project['models'][number]) => model.name === name)
			? name
			: null;
	}
	const byRelation: Project['models'][number] | undefined = project.models.find(
		(model: Project['models'][number]) =>
			stepId.endsWith(`_${model.relationName}`) || stepId.endsWith(`_mv__${model.name}`)
	);
	return byRelation?.name ?? null;
}

function buildNotes(
	runGraph: Graph,
	modelStates: Map<string, ModelRunState>,
	running: boolean,
	outcome: string,
	operation: RunGraphOperation,
	executedIds: Set<string>
): Map<string, RunNodeNote> {
	const notes: Map<string, RunNodeNote> = new Map();
	for (const node of runGraph.nodes) {
		if (operation === 'destruction' && executedIds.has(node.id)) {
			notes.set(node.id, {
				text: running ? 'removing…' : outcome === 'succeeded' ? 'removed' : 'incomplete',
				tone: running || outcome === 'succeeded' ? 'info' : 'warn'
			});
			continue;
		}
		const state: ModelRunState | undefined = modelStates.get(node.logicalName);
		if (state === undefined) continue;
		if (state.state === 'failed') {
			notes.set(node.id, { text: 'failed', tone: 'warn' });
		} else if (state.state === 'running') {
			notes.set(node.id, {
				text: running
					? operation === 'promotion'
						? 'switching…'
						: 'rebuilding…'
					: outcome === 'succeeded'
						? operation === 'promotion'
							? 'switched'
							: 'rebuilt'
						: 'incomplete',
				tone: running || outcome === 'succeeded' ? 'info' : 'warn'
			});
		} else {
			notes.set(node.id, {
				text:
					state.rows === null
						? operation === 'promotion'
							? 'switched'
							: 'rebuilt'
						: `${formatCompact(state.rows)} rows`,
				tone: 'info'
			});
		}
	}
	return notes;
}
