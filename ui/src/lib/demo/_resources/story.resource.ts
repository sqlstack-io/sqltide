import { DEMO_OFF_STORY_MESSAGE, DEMO_SELECTABLE, DEMO_UNPREPARED_SELECTION_MESSAGE } from '../constants';
import { fillSlots, startRole } from '../_helpers/fill-slots';
import { planSelection, selectionKey } from '../_helpers/selection-key';
import type { PlanSelection } from '../_helpers/selection-key';
import type {
	BuildRequest,
	DemoAnswer,
	DemoStory,
	PreparedAction,
	PreparedBinding,
	PreparedDemo,
	PreparedState,
	SlotValues
} from '../types';

/**
 * The story's state machine on the engine. sqt worked out every plan and every action's SQL
 * ahead of time (scripts/prepare-demo.mjs); this fills in the values that differ in each tab,
 * such as deployment IDs and times, and runs the SQL on the engine in order.
 */
export function createStoryResource(prepared: PreparedDemo, run: (sql: string) => Promise<unknown>): DemoStory {
	const values: SlotValues = {
		deployments: {},
		uuids: {},
		roleStarts: {},
		times: {},
		version: prepared.preparedWith,
		startTime: null
	};
	let stateName: string | null = prepared.start;
	let acting: boolean = false;
	let lastBuild: { invocationId: string; command: string; running: boolean; exitCode: number | null; stderr: string } | null =
		null;

	const fill: <Value>(value: Value) => Value = <Value>(value: Value): Value =>
		JSON.parse(fillSlots(JSON.stringify(value), values)) as Value;
	const answer: (status: number, detail: string) => DemoAnswer = (status, detail) => ({ status, body: { detail } });
	const state: () => PreparedState | null = () => (stateName === null ? null : prepared.states[stateName]);
	const sql: (indexes: number[]) => string[] = (indexes) =>
		indexes.map((index) => fillSlots(prepared.statements[index], values));

	function startFrom(startTime: string | null): void {
		values.startTime = startTime === null ? null : Date.parse(startTime);
	}

	/** Why a selection has nothing prepared: names the demo does not have, or a step off the story. */
	function unprepared(selection: PlanSelection): DemoAnswer {
		if (acting) return answer(409, 'An action is still running on the engine. Try again when it finishes.');
		if (state() === null) return answer(409, DEMO_OFF_STORY_MESSAGE);
		const unknown: string[] = selection.selectors.filter((selector) => !DEMO_SELECTABLE.includes(selector));
		const named: string = unknown.length === 0 ? 'this selection' : unknown.map((selector) => `\`${selector}\``).join(', ');
		return answer(400, `The demo has no plan prepared for ${named}. ${DEMO_UNPREPARED_SELECTION_MESSAGE}`);
	}

	/**
	 * Runs an action's statements in order, one action at a time as `sqt dev` does; the story moves
	 * on to the action's next state once they all ran. The slots are filled before it starts.
	 */
	async function act(action: PreparedAction, statements: string[]): Promise<void> {
		acting = true;
		stateName = null;
		try {
			for (const statement of statements) await run(statement);
			stateName = action.next;
		} finally {
			acting = false;
		}
	}

	async function replaySetup(onStep: (title: string, done: number, total: number) => void): Promise<void> {
		const total: number = prepared.setup.reduce((sum, step) => sum + step.statements.length, 0);
		let done: number = 0;
		for (const step of prepared.setup) {
			startRole(values, step.role, Date.now(), step);
			for (const statement of sql(step.statements)) {
				onStep(step.title, done, total);
				await run(statement);
				done += 1;
			}
		}
		onStep('Ready', done, total);
	}

	return {
		replaySetup,

		async startOver(): Promise<void> {
			if (acting) throw new Error('An action is still running.');
			acting = true;
			stateName = null;
			try {
				await run(`DROP DATABASE IF EXISTS ${prepared.database}`);
				Object.assign(values, { deployments: {}, uuids: {}, roleStarts: {}, times: {}, startTime: null });
				lastBuild = null;
				await replaySetup(() => undefined);
				stateName = prepared.start;
			} finally {
				acting = false;
			}
		},

		plan(params: URLSearchParams): DemoAnswer {
			const selection: PlanSelection = planSelection(params);
			const plan: (DemoAnswer & PreparedBinding) | undefined =
				state()?.plans[`${selectionKey(selection)}|${params.get('counts') === 'true' ? 'counts' : ''}`];
			if (plan === undefined) return unprepared(selection);
			const pinned: string | null = params.get('deployment');
			startRole(values, 'plan', Date.now(), plan, pinned === null ? {} : { planned: pinned });
			startFrom(selection.startTime);
			return fill({ status: plan.status, body: plan.body });
		},

		async build(request: BuildRequest): Promise<DemoAnswer> {
			const selection: PlanSelection = {
				selectors: request.selectors,
				changed: request.changed ?? false,
				includeMissingUpstream: request.includeMissingUpstream ?? false,
				startTime: request.startTime
			};
			const action: PreparedAction | undefined = state()?.actions[`build ${selectionKey(selection)}`];
			if (acting) return answer(409, 'A build is already running.');
			if (action === undefined) return unprepared(selection);
			const ids: Record<string, string> = {};
			if (request.deploymentId && action.creates.length === 1) ids[action.creates[0].name] = request.deploymentId;
			startFrom(selection.startTime);
			// The response names the run before it starts, as sqt's does; the run page then follows it.
			startRole(values, action.role, Date.now(), action, ids);
			const response: DemoAnswer = fill(action.response);
			const invocationId: string = (response.body as { invocationId: string }).invocationId;
			const command: string = ['sqt build', ...selection.selectors.map((selector) => `--select ${selector}`)].join(' ');
			const statements: string[] = sql(action.statements);
			lastBuild = { invocationId, command, running: true, exitCode: null, stderr: '' };
			const build: { running: boolean; exitCode: number | null; stderr: string } = lastBuild;
			void act(action, statements).then(
				() => Object.assign(build, { running: false, exitCode: 0 }),
				(error: unknown) => {
					console.error('The demo engine stopped partway through the build:', error);
					Object.assign(build, { running: false, exitCode: 1, stderr: error instanceof Error ? error.message : String(error) });
				}
			);
			return response;
		},

		async deploymentAction(kind: 'promote' | 'rollback', deploymentId: string): Promise<DemoAnswer> {
			// The plan's proposed deployment and the build that made it share one ID; take the name
			// this state has an action for.
			const name: string | undefined = Object.keys(values.deployments).find(
				(candidate) => values.deployments[candidate].id === deploymentId && state()?.actions[`${kind} ${candidate}`] !== undefined
			);
			const action: PreparedAction | undefined = name === undefined ? undefined : state()?.actions[`${kind} ${name}`];
			if (acting) return answer(409, 'Another action is still running.');
			if (action === undefined) {
				return answer(409, state() === null ? DEMO_OFF_STORY_MESSAGE : `The demo has no ${kind === 'promote' ? 'promotion' : 'rollback'} prepared for ${deploymentId} at this step of the story. Run \`sqt playground\` to try it with ClickHouse on your machine.`);
			}
			startRole(values, action.role, Date.now(), action);
			const response: DemoAnswer = fill(action.response);
			await act(action, sql(action.statements));
			return response;
		},

		buildFeed(): unknown {
			return {
				running: lastBuild?.running ?? false,
				invocationId: lastBuild?.invocationId ?? null,
				currentInvocationId: lastBuild?.running ? lastBuild.invocationId : null,
				command: lastBuild?.command ?? '',
				exitCode: lastBuild?.exitCode ?? null,
				events: [],
				stderr: lastBuild?.stderr ?? '',
				forceAvailable: false,
				activeQueryId: null,
				cancellationStatus: null,
				cancellationError: null,
				cancellationQueryId: null
			};
		},

		get stateName(): string | null {
			return stateName;
		}
	};
}
