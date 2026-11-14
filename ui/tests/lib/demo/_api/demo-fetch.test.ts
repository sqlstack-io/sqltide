import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import {
	DEMO_ONE_ACTION_MESSAGE,
	DEMO_READ_ONLY_MESSAGE,
	installDemoFetch,
	type DemoEngineLink
} from '$lib/demo/_api/demo-fetch';
import type { TableSize } from '$lib/demo/_engine/engine';
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
	const staged: string = recordings.promotion.deploymentId;
	let promoted: boolean;
	let promote: MockInstance<() => Promise<void>> & (() => Promise<void>);

	beforeEach(() => {
		promoted = false;
		promote = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
		// Every table in the engine holds 7 rows and 100 bytes, whatever was recorded.
		const sizes = { get: () => ({ rows: 7, bytes: 100 }) } as unknown as Map<string, TableSize>;
		const engine = { promote, tableSizes: async () => sizes };
		const link: DemoEngineLink = {
			active: () => true,
			ready: () => engine,
			whenReady: async () => engine,
			promoted: () => promoted,
			markPromoted: () => {
				promoted = true;
			}
		};
		vi.stubGlobal('fetch', vi.fn());
		vi.stubGlobal('location', new URL('https://sqltide.io/demo/deployments'));
		installDemoFetch(link);
	});

	afterEach(() => vi.unstubAllGlobals());

	it('given the staged deployment when promoted then the engine promotes and later GETs answer from after the promotion', async () => {
		const before = await (await fetch('/api/deployments')).json();

		const response: Response = await fetch(`/api/deployments/${staged}/promote`, { method: 'POST' });
		const after = await (await fetch('/api/deployments')).json();

		expect(promote).toHaveBeenCalledOnce();
		expect(await response.json()).toEqual(recordings.promotion.response.body);
		const state = (body: { deployments: { deploymentId: string; state: string }[] }): string | undefined =>
			body.deployments.find((deployment) => deployment.deploymentId === staged)?.state;
		expect([state(before), state(after)]).toEqual(['staged', 'active']);
	});

	it('given a deployment list when fetched then each size is the sum of its tables in the engine', async () => {
		const { deployments } = await (await fetch('/api/deployments')).json();

		for (const deployment of deployments) {
			expect(deployment.rows).toBe(7 * deployment.physicalRelationNames.length);
			expect(deployment.bytes).toBe(100 * deployment.physicalRelationNames.length);
		}
	});

	it('given any other write, or a second promotion, when fetched then it is refused without touching the engine', async () => {
		await fetch(`/api/deployments/${staged}/promote`, { method: 'POST' });
		promote.mockClear();

		const again: Response = await fetch(`/api/deployments/${staged}/promote`, { method: 'POST' });
		const cleanup: Response = await fetch('/api/deployments/cleanup', { method: 'POST' });

		expect([again.status, cleanup.status]).toEqual([403, 403]);
		expect(await cleanup.json()).toEqual({ detail: DEMO_ONE_ACTION_MESSAGE });
		expect(promote).not.toHaveBeenCalled();
	});
});
