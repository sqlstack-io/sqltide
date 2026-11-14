import recordings from '../recordings.json';

/** What every write in the demo returns, and what the UI shows for it. */
export const DEMO_READ_ONLY_MESSAGE: string =
	'This is a read-only UI demo with example data. Run `sqt playground` to try it with ClickHouse on your machine.';

type Recording = { status: number; body: unknown };

function reply(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/**
 * Serves the UI's `/api` requests from the recorded responses: GETs return what `sqt dev`
 * returned for the same path and query, and writes return the read-only message. A GET with no
 * recording fails loudly, so a UI change that needs a new endpoint is re-recorded.
 */
export function installDemoFetch(): void {
	const responses: Record<string, Recording> = recordings.responses;
	const passThrough: typeof fetch = globalThis.fetch.bind(globalThis);
	globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const url: URL = new URL(
			input instanceof Request ? input.url : String(input),
			globalThis.location.href
		);
		if (url.origin !== globalThis.location.origin || !url.pathname.startsWith('/api/')) {
			return passThrough(input, init);
		}
		const method: string = init?.method ?? (input instanceof Request ? input.method : 'GET');
		if (method.toUpperCase() !== 'GET') return reply(403, { detail: DEMO_READ_ONLY_MESSAGE });
		const recording: Recording | undefined = responses[url.pathname + url.search];
		if (recording === undefined) {
			console.error(`UI demo has no recording for GET ${url.pathname}${url.search}`);
			return reply(404, { detail: `No recording for ${url.pathname}` });
		}
		return reply(recording.status, recording.body);
	};
}
