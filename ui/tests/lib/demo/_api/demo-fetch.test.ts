import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import { DEMO_READ_ONLY_MESSAGE, installDemoFetch } from '$lib/demo/_api/demo-fetch';
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
