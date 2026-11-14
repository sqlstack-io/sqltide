import { base } from '$app/paths';
import { goto } from '$app/navigation';
import {
	createDestructionPlan
} from '$lib/api/main/destruction/create-destruction-plan';
import type {
	DestructionOperation,
	DestructionPlan
} from '$lib/pipeline-view/types';
import { getInactivePipelines } from '$lib/inactive-pipelines/main/get-inactive-pipelines';
import type { InactivePipeline } from '$lib/pipeline-view/types';
import type { DestructionController } from './types';

export function createDestructionState(): DestructionController {
	let open = $state<boolean>(false);
	let operation = $state<DestructionOperation | null>(null);
	let plan = $state<DestructionPlan | null>(null);
	let planning = $state<boolean>(false);
	let error = $state<string | null>(null);
	let inactivePipelines = $state<InactivePipeline[]>([]);
	let loadingInactivePipelines = $state<boolean>(false);
	let inactivePipelinesError = $state<string | null>(null);

	function setOpen(value: boolean): void {
		if (!value && planning) return;
		open = value;
		if (!value) resetPlan();
	}

	async function start(
		nextOperation: DestructionOperation,
		pipelineNames: string[] = []
	): Promise<void> {
		open = true;
		operation = nextOperation;
		await loadPlan(
			nextOperation,
			nextOperation === 'destroy_pipelines' ? [...pipelineNames].sort() : [],
			[]
		);
	}

	async function loadPlan(
		nextOperation: DestructionOperation,
		pipelineNames: string[],
		includedDependentPipelineNames: string[]
	): Promise<void> {
		planning = true;
		plan = null;
		error = null;
		try {
			const created: DestructionPlan = await createDestructionPlan(
				nextOperation,
				pipelineNames,
				includedDependentPipelineNames
			);
			plan = created;
			if (created.blocked) {
				return;
			}
			await goto(`${base}/destruction/plans/${encodeURIComponent(created.planId)}`);
			open = false;
			resetPlan();
		} catch (caught) {
			error = String(caught);
		} finally {
			planning = false;
		}
	}

	async function addRequiredDependentsAndReplan(): Promise<void> {
		if (plan === null || operation !== 'destroy_pipelines') return;
		await loadPlan(
			operation,
			[...plan.selectedPipelines].sort(),
			[...plan.requiredDependentPipelines].sort()
		);
	}

	function resetPlan(): void {
		operation = null;
		plan = null;
		error = null;
	}

	async function loadInactivePipelines(): Promise<void> {
		if (loadingInactivePipelines) return;
		loadingInactivePipelines = true;
		inactivePipelinesError = null;
		try {
			inactivePipelines = await getInactivePipelines();
		} catch (caught) {
			inactivePipelinesError = String(caught);
		} finally {
			loadingInactivePipelines = false;
		}
	}

	return {
		get open() {
			return open;
		},
		get operation() {
			return operation;
		},
		get plan() {
			return plan;
		},
		get planning() {
			return planning;
		},
		get error() {
			return error;
		},
		get inactivePipelines() {
			return inactivePipelines;
		},
		get loadingInactivePipelines() {
			return loadingInactivePipelines;
		},
		get inactivePipelinesError() {
			return inactivePipelinesError;
		},
		setOpen,
		start,
		addRequiredDependentsAndReplan,
		loadInactivePipelines
	};
}
