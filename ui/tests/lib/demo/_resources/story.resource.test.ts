import { describe, expect, it } from 'vitest';

import { DEMO_OFF_STORY_MESSAGE } from '$lib/demo/constants';
import { createStoryResource } from '$lib/demo/_resources/story.resource';
import type { DemoAnswer, DemoStory, PreparedDemo } from '$lib/demo/types';

/**
 * A two-state story: publishing v1 creates it; Execute on the pipeline stages `built`, and
 * promoting `built` leaves the prepared states.
 */
function story(): PreparedDemo {
	return {
		preparedWith: 'sqt test',
		database: 'db',
		start: 'nothing-staged',
		statements: [
			'CREATE TABLE db.t__{{d:v1}}',
			"INSERT INTO db.runs VALUES ('{{u:build.0}}', '{{d:built}}', '{{t:build:0:s3}}', '{{s:s3}}')",
			"INSERT INTO db.runs VALUES ('{{u:build.0}}', 'done', '{{t:build:1:s3}}', '{{v}}')",
			"INSERT INTO db.promotions VALUES ('{{u:promote.0}}', '{{d:built}}', '{{d:v1}}')"
		],
		setup: [{ role: 'publish1', title: 'Publish', statements: [0], creates: [{ name: 'v1', offset: 0 }], times: [] }],
		states: {
			'nothing-staged': {
				plans: {
					'pipeline:pl__orders||from|': {
						status: 200,
						body: { deploymentId: '{{d:planned}}', startTime: '{{s:s3}}' },
						creates: [{ name: 'planned', offset: 0 }],
						times: []
					}
				},
				actions: {
					'build pipeline:pl__orders||from': {
						role: 'build',
						response: { status: 200, body: { invocationId: '{{u:build.0}}' } },
						statements: [1, 2],
						creates: [{ name: 'built', offset: 0 }],
						times: [
							['build', 0, 10],
							['build', 1, 900]
						],
						next: 'staged'
					}
				}
			},
			staged: {
				plans: {},
				actions: {
					'promote built': {
						role: 'promote',
						response: { status: 200, body: { invocationId: '{{u:promote.0}}', deploymentId: '{{d:built}}' } },
						statements: [3],
						creates: [],
						times: [],
						next: null
					}
				}
			}
		}
	};
}

async function started(ran: string[]): Promise<DemoStory> {
	const created: DemoStory = createStoryResource(story(), async (sql: string) => {
		ran.push(sql);
	});
	await created.replaySetup(() => undefined);
	return created;
}

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
const PLAN_QUERY: string = 'select=pipeline%3Apl__orders&start=2026-01-01T10%3A01%3A00Z';

describe('story', () => {
	it('given a plan then Execute when run then the build runs the SQL sqt wrote for that plan, with its values filled', async () => {
		const ran: string[] = [];
		const demo: DemoStory = await started(ran);

		const plan: DemoAnswer = demo.plan(new URLSearchParams(PLAN_QUERY));
		const { deploymentId } = plan.body as { deploymentId: string };
		const build: DemoAnswer = await demo.build({
			selectors: ['pipeline:pl__orders'],
			startTime: '2026-01-01T10:01:00Z',
			deploymentId,
			confirmations: []
		});
		await flush();

		const { invocationId } = build.body as { invocationId: string };
		expect(plan.body).toEqual({ deploymentId, startTime: '2026-01-01 10:01:00.000' });
		expect(ran[0]).toMatch(/^CREATE TABLE db\.t__\d{8}T\d{6}Z_[0-9a-f]{6}$/);
		expect(ran[1]).toContain(`('${invocationId}', '${deploymentId}', `);
		expect(ran[1]).toContain(", '2026-01-01 10:01:00.000')");
		expect(ran[2]).toContain(`('${invocationId}', 'done', `);
		expect(ran[2]).toContain(", 'sqt test')");
		expect(demo.stateName).toBe('staged');
		expect(demo.buildFeed()).toMatchObject({ running: false, invocationId, exitCode: 0 });
	});

	it('given the staged deployment when promoted then the promotion runs and the story leaves its prepared states', async () => {
		const ran: string[] = [];
		const demo: DemoStory = await started(ran);
		await demo.build({ selectors: ['pipeline:pl__orders'], startTime: '2026-01-01T10:01:00Z', confirmations: [] });
		await flush();
		const built: string = ran[1].split("', '")[1];

		const promoted: DemoAnswer = await demo.deploymentAction('promote', built);

		expect(promoted.status).toBe(200);
		expect((promoted.body as { deploymentId: string }).deploymentId).toBe(built);
		expect(ran[3]).toContain(`'${built}', '${ran[0].slice('CREATE TABLE db.t__'.length)}')`);
		expect(demo.stateName).toBeNull();
		expect(demo.plan(new URLSearchParams(PLAN_QUERY))).toEqual({ status: 409, body: { detail: DEMO_OFF_STORY_MESSAGE } });
	});

	it('given Execute on the deployment the plan proposed when promoted then that deployment is the one promoted', async () => {
		const ran: string[] = [];
		const demo: DemoStory = await started(ran);
		const { deploymentId } = demo.plan(new URLSearchParams(PLAN_QUERY)).body as { deploymentId: string };
		demo.plan(new URLSearchParams(`${PLAN_QUERY}&deployment=${deploymentId}`));
		await demo.build({ selectors: ['pipeline:pl__orders'], startTime: '2026-01-01T10:01:00Z', deploymentId, confirmations: [] });
		await flush();

		const promoted: DemoAnswer = await demo.deploymentAction('promote', deploymentId);

		expect(promoted).toMatchObject({ status: 200, body: { deploymentId } });
	});

	it('given a build still running when a plan is asked for then it says to wait, not to start over', async () => {
		let release: () => void = () => undefined;
		const demo: DemoStory = createStoryResource(story(), (sql: string) =>
			sql.startsWith('INSERT') ? new Promise<void>((resolve) => (release = resolve)) : Promise.resolve()
		);
		await demo.replaySetup(() => undefined);
		await demo.build({ selectors: ['pipeline:pl__orders'], startTime: '2026-01-01T10:01:00Z', confirmations: [] });

		const answer: DemoAnswer = demo.plan(new URLSearchParams(PLAN_QUERY));
		release();

		expect(answer.status).toBe(409);
		expect((answer.body as { detail: string }).detail).toContain('still running');
	});

	it('given a selection with a name the demo does not have when planned then it says so and names it', async () => {
		const demo: DemoStory = await started([]);

		const answer: DemoAnswer = demo.plan(new URLSearchParams('select=orders_by_day&select=order_report'));

		expect(answer.status).toBe(400);
		expect((answer.body as { detail: string }).detail).toMatch(/^The demo has no plan prepared for `orders_by_day`\. .*sqt playground/);
	});

	it('given a deployment with no prepared action here when promoted then it is refused and nothing runs', async () => {
		const ran: string[] = [];
		const demo: DemoStory = await started(ran);

		const answer: DemoAnswer = await demo.deploymentAction('rollback', '20990101T000000Z_aaaaaa');

		expect(answer.status).toBe(409);
		expect(ran).toHaveLength(1);
		expect(demo.stateName).toBe('nothing-staged');
	});

	it('given a statement the engine refuses when building then the build fails with its error and the story stops', async () => {
		let calls: number = 0;
		const demo: DemoStory = createStoryResource(story(), async () => {
			calls += 1;
			if (calls === 3) throw new Error('Code: 60. Unknown table');
		});
		await demo.replaySetup(() => undefined);
		const failures: unknown[] = [];
		const original: typeof console.error = console.error;
		console.error = (...args: unknown[]) => failures.push(args);

		await demo.build({ selectors: ['pipeline:pl__orders'], startTime: '2026-01-01T10:01:00Z', confirmations: [] });
		await flush();
		console.error = original;

		expect(demo.buildFeed()).toMatchObject({ running: false, exitCode: 1, stderr: 'Code: 60. Unknown table' });
		expect(demo.stateName).toBeNull();
		expect(failures).toHaveLength(1);
	});
});
