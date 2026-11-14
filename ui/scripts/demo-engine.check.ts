/**
 * Runs everything the UI demo prepared on chdb-wasm, the engine the demo runs in the browser: the
 * story's history, then every action in every state, each from a fresh replay of how the story
 * got there. Every statement must run once its slots are filled, and every plan must fill too.
 *
 *   CHDB_WASM=../website/node_modules/chdb-wasm npx vitest run --config scripts/demo-engine.config.ts
 */
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';

import prepared from '../src/lib/demo/prepared.json';
import { createStoryResource } from '../src/lib/demo/_resources/story.resource';
import { readDeployments } from '../src/lib/demo-readers/main/read-deployments';
import type { DemoAnswer, DemoStory, PreparedAction, PreparedDemo } from '../src/lib/demo/types';

type Chdb = { query(sql: string, format?: string): Promise<{ text(): string }>; terminate(): Promise<void> };
type Engine = { run: (sql: string) => Promise<string>; stop: () => Promise<void> };

const demo: PreparedDemo = prepared as unknown as PreparedDemo;
const directory: string = pathToFileURL(`${resolve(process.env.CHDB_WASM ?? '../website/node_modules/chdb-wasm')}/`).href;

/** An engine whose queries run one at a time, as the demo runs them. */
async function startEngine(): Promise<Engine> {
	const { AsyncChdb } = (await import(/* @vite-ignore */ `${directory}dist/index.js`)) as {
		AsyncChdb: { create(options: { moduleUrl: string }): Promise<Chdb> };
	};
	const client: Chdb = await AsyncChdb.create({ moduleUrl: `${directory}dist/chdb.mjs` });
	let queue: Promise<unknown> = Promise.resolve();
	const run: (sql: string) => Promise<string> = (sql: string): Promise<string> => {
		const turn: Promise<string> = queue.then(async () => {
			try {
				return (await client.query(sql)).text();
			} catch (error) {
				throw new Error(`${error instanceof Error ? error.message : String(error)}\n  in: ${sql.slice(0, 400)}`);
			}
		});
		queue = turn.catch(() => undefined);
		return turn;
	};
	return { run, stop: () => client.terminate() };
}

/** The actions that lead from the start to each state, by breadth-first search. */
function paths(): Map<string, { state: string; key: string }[]> {
	const found: Map<string, { state: string; key: string }[]> = new Map([[demo.start, []]]);
	const queue: string[] = [demo.start];
	while (queue.length > 0) {
		const state: string = queue.shift() as string;
		for (const [key, action] of Object.entries(demo.states[state].actions) as [string, PreparedAction][]) {
			if (action.next === null || found.has(action.next)) continue;
			found.set(action.next, [...(found.get(state) ?? []), { state, key }]);
			queue.push(action.next);
		}
	}
	return found;
}

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

/** The story's deployments in the order it created them, as the Deployments page lists them. */
const CREATED: string[] = ['v1', 'v2', 'built'];

/** Runs one prepared action the way the API does, and waits for a build to finish. */
async function act(story: DemoStory, run: (sql: string) => Promise<string>, key: string): Promise<DemoAnswer> {
	const [kind, ...rest] = key.split(' ');
	if (kind === 'promote' || kind === 'rollback') {
		const rows: <Row>(sql: string) => Promise<Row[]> = async <Row>(sql: string): Promise<Row[]> =>
			(await run(`${sql} FORMAT JSONEachRow`))
				.split('\n')
				.filter((line) => line.trim() !== '')
				.map((line) => JSON.parse(line) as Row);
		const { deployments } = await readDeployments(rows, demo.database);
		const ids: string[] = deployments.map((deployment) => deployment.deploymentId).sort();
		return story.deploymentAction(kind, ids[CREATED.indexOf(rest[0])]);
	}
	const [selectors, upstream, from] = rest.join(' ').split('|');
	const select: string[] = selectors === '' ? [] : selectors.split(' ');
	const startTime: string | null = from === 'from' ? '2026-01-01T10:01:00Z' : null;
	// As the Plan page does: plan, then execute the deployment the plan proposed.
	const params: URLSearchParams = new URLSearchParams(select.map((selector) => ['select', selector]));
	if (upstream === 'upstream') params.set('include_missing_upstream', 'true');
	if (startTime !== null) params.set('start', startTime);
	const { deploymentId } = story.plan(params).body as { deploymentId?: string };
	const answer: DemoAnswer = await story.build({
		selectors: select,
		includeMissingUpstream: upstream === 'upstream',
		startTime,
		...(deploymentId === undefined ? {} : { deploymentId }),
		confirmations: []
	});
	while ((story.buildFeed() as { running: boolean }).running) await flush();
	const feed: { exitCode: number | null; stderr: string } = story.buildFeed() as { exitCode: number | null; stderr: string };
	if (feed.exitCode !== 0) throw new Error(`build failed: ${feed.stderr}`);
	return answer;
}

describe('the UI demo on its engine', () => {
	// chdb keeps dropped tables for 8 minutes, so each state gets a fresh engine: starting over
	// for every action in all of them would outgrow the engine's memory.
	it('runs the story and every prepared action in every state', { timeout: 3_600_000 }, async () => {
		const routes: Map<string, { state: string; key: string }[]> = paths();
		expect([...routes.keys()].sort()).toEqual(Object.keys(demo.states).sort());
		let ran: number = 0;
		for (const [state, route] of routes) {
			const { run, stop }: Engine = await startEngine();
			const story: DemoStory = createStoryResource(demo, run);
			await story.replaySetup(() => undefined);
			for (const key of Object.keys(demo.states[state].actions)) {
				await story.startOver();
				for (const step of route) await act(story, run, step.key);
				expect(story.stateName).toBe(state);
				// Every plan here fills its slots, as the Plan page asks for it.
				for (const plan of Object.keys(demo.states[state].plans)) {
					const [selectors, upstream, from, counts] = plan.split('|');
					const params: URLSearchParams = new URLSearchParams();
					if (selectors === 'changed') params.set('changed', 'true');
					else for (const selector of selectors.split(' ').filter(Boolean)) params.append('select', selector);
					if (upstream === 'upstream') params.set('include_missing_upstream', 'true');
					if (from === 'from') params.set('start', '2026-01-01T10:01:00Z');
					if (counts === 'counts') params.set('counts', 'true');
					expect(JSON.stringify(story.plan(params))).not.toMatch(/\{\{/);
				}
				const answer: DemoAnswer = await act(story, run, key);
				expect(answer.status, `${state}: ${key}`).toBe(200);
				expect(JSON.stringify(answer)).not.toMatch(/\{\{/);
				expect(story.stateName).toBe(demo.states[state].actions[key].next);
				// The query the demo's panel shows still answers.
				expect(Number(await run(`SELECT count() FROM ${demo.database}.tbl__order_report`))).toBeGreaterThan(0);
				ran += 1;
				console.log(`${ran} ${state}: ${key} (${Math.round(performance.now() / 1000)} s)`);
			}
			await stop();
		}
		expect(ran).toBe(Object.values(demo.states).reduce((sum, state) => sum + Object.keys(state.actions).length, 0));
	});
});
