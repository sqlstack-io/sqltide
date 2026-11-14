import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import { installDemoFetch } from '$lib/demo/_api/demo-fetch';
import { DEMO_NOT_IN_BROWSER_MESSAGE, DEMO_READ_ONLY_MESSAGE } from '$lib/demo/constants';
import type { DemoAnswer, DemoEngineLink, DemoStory, LiveDemo } from '$lib/demo/types';
import recordings from '$lib/demo/recordings.json';

describe('UI demo fetch', () => {
	let passThrough: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		passThrough = vi.fn().mockResolvedValue(new Response('site'));
		vi.stubGlobal('fetch', passThrough);
		vi.stubGlobal('location', new URL('https://sqltide.io/demo/deployments'));
		installDemoFetch();
	});

	afterEach(() => vi.unstubAllGlobals());

	it('given a recorded GET when fetched then the recorded status and body are returned', async () => {
		const [key, recording] = Object.entries(recordings.responses)[0];

		const response: Response = await fetch(key);

		expect(response.status).toBe(recording.status);
		expect(await response.json()).toEqual(recording.body);
		expect(passThrough).not.toHaveBeenCalled();
	});

	it('given a write when fetched then it is refused with the read-only message', async () => {
		const response: Response = await fetch('/api/deployments/any/promote', { method: 'POST' });

		expect(response.status).toBe(403);
		expect(await response.json()).toEqual({ detail: DEMO_READ_ONLY_MESSAGE });
	});

	it('given a GET with no recording when fetched then it fails and logs the request', async () => {
		const logged: MockInstance<typeof console.error> = vi.spyOn(console, 'error').mockImplementation(() => undefined);

		const response: Response = await fetch('/api/status?unrecorded=1');

		expect(response.status).toBe(404);
		expect(logged).toHaveBeenCalledWith('UI demo has no recording for GET /api/status?unrecorded=1');
	});

	it('given a request outside the API when fetched then it reaches the network', async () => {
		await fetch('/demo/_app/version.json');

		expect(passThrough).toHaveBeenCalledOnce();
	});
});

describe('UI demo fetch with the real engine', () => {
	let story: { [Name in 'plan' | 'build' | 'deploymentAction']: ReturnType<typeof vi.fn> };

	beforeEach(() => {
		const answer: DemoAnswer = { status: 200, body: { from: 'story' } };
		story = {
			plan: vi.fn().mockReturnValue(answer),
			build: vi.fn().mockResolvedValue(answer),
			deploymentAction: vi.fn().mockResolvedValue(answer)
		};
		const live: LiveDemo = { engine: { database: 'db', rows: vi.fn() }, story: story as unknown as DemoStory } as unknown as LiveDemo;
		vi.stubGlobal('fetch', vi.fn());
		vi.stubGlobal('location', new URL('https://sqltide.io/demo/plan'));
		installDemoFetch({ active: () => true, whenReady: async () => live });
	});

	afterEach(() => vi.unstubAllGlobals());

	it('given a plan request when fetched then the story answers it with its query', async () => {
		const response: Response = await fetch('/api/plan?select=order_report&counts=true');

		expect(await response.json()).toEqual({ from: 'story' });
		expect(String(story.plan.mock.calls[0][0])).toBe('select=order_report&counts=true');
	});

	it('given Execute, Promote and Roll back when posted then the story runs each', async () => {
		const request: object = { selectors: ['order_report'], startTime: null, confirmations: [] };

		await fetch('/api/build', { method: 'POST', body: JSON.stringify(request) });
		await fetch('/api/deployments/20261114T082312Z_0a1b2c/promote', { method: 'POST' });
		await fetch('/api/deployments/20261114T082312Z_0a1b2c/rollback', { method: 'POST' });

		expect(story.build).toHaveBeenCalledWith(request);
		expect(story.deploymentAction.mock.calls).toEqual([
			['promote', '20261114T082312Z_0a1b2c'],
			['rollback', '20261114T082312Z_0a1b2c']
		]);
	});

	it('given what the engine does not answer, such as auth, when fetched while the engine loads then the recording answers at once', async () => {
		vi.stubGlobal('location', new URL('https://sqltide.io/demo/'));
		installDemoFetch({ active: () => true, whenReady: () => new Promise<never>(() => undefined) });

		const response: Response = await fetch('/api/auth/me');

		expect(await response.json()).toEqual(recordings.responses['/api/auth/me'].body);
	});

	it('given a write the browser demo does not run when posted then it says so and points to the playground', async () => {
		const response: Response = await fetch('/api/deployments/cleanup', { method: 'POST' });

		expect(response.status).toBe(403);
		expect(await response.json()).toEqual({ detail: DEMO_NOT_IN_BROWSER_MESSAGE });
	});
});

describe('UI demo fetch when the engine cannot start', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('given the visitor chose the recording when a page asks then the recording answers and writes stay read-only', async () => {
		// The engine failed while the page loaded; its requests waited, then the visitor chose the recording.
		const link: DemoEngineLink = { active: () => true, whenReady: async () => null };
		vi.stubGlobal('fetch', vi.fn());
		vi.stubGlobal('location', new URL('https://sqltide.io/demo/'));
		installDemoFetch(link);

		const status: Response = await fetch('/api/status');
		const promote: Response = await fetch('/api/deployments/any/promote', { method: 'POST' });

		expect(await status.json()).toEqual(recordings.responses['/api/status'].body);
		expect(await promote.json()).toEqual({ detail: DEMO_READ_ONLY_MESSAGE });
	});
});
