import recordings from '../recordings.json';
import { DEMO_ENGINE_GETS, DEMO_NOT_IN_BROWSER_MESSAGE, DEMO_READ_ONLY_MESSAGE } from '../constants';
import { answerFromEngine } from './engine-answers';
import type { BuildRequest, DemoAnswer, DemoEngineLink, LiveDemo } from '../types';

function reply({ status, body }: DemoAnswer): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

const failure = (error: unknown): DemoAnswer => ({
	status: 500,
	body: { detail: `The demo engine failed: ${error instanceof Error ? error.message : String(error)}` }
});

const DEPLOYMENT_ACTION: RegExp = /^\/api\/deployments\/([^/]+)\/(promote|rollback)$/;

/** A write the real engine runs: Execute, Promote and Roll back, which sqt prepared. */
async function write(live: LiveDemo, url: URL, body: string): Promise<DemoAnswer> {
	const deploymentAction: RegExpMatchArray | null = url.pathname.match(DEPLOYMENT_ACTION);
	if (deploymentAction !== null) {
		return live.story.deploymentAction(
			deploymentAction[2] as 'promote' | 'rollback',
			decodeURIComponent(deploymentAction[1])
		);
	}
	if (url.pathname === '/api/build') return live.story.build(JSON.parse(body) as BuildRequest);
	// Reloading the project or the warehouse status changes nothing here; both answer the status.
	if (url.pathname === '/api/reload' || url.pathname === '/api/warehouse/refresh') {
		return recordings.responses['/api/status'];
	}
	return { status: 403, body: { detail: DEMO_NOT_IN_BROWSER_MESSAGE } };
}

/**
 * Serves the UI's `/api` requests for the UI demo build.
 *
 * With the real engine, sqt's plans and actions come from the story it prepared, and every page
 * that shows the warehouse reads the engine (see answerFromEngine). Requests made while the
 * engine loads wait for it; if it cannot start, they wait for the visitor to choose the recording.
 *
 * Without it, GETs return what `sqt dev` returned for the same path and query, and writes return
 * the read-only message. A GET with no recording fails loudly, so a UI change that needs a new
 * endpoint is re-recorded.
 */
export function installDemoFetch(engine: DemoEngineLink | null = null): void {
	const responses: Record<string, DemoAnswer> = recordings.responses;
	const passThrough: typeof fetch = globalThis.fetch.bind(globalThis);
	globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const url: URL = new URL(input instanceof Request ? input.url : String(input), globalThis.location.href);
		if (url.origin !== globalThis.location.origin || !url.pathname.startsWith('/api/')) {
			return passThrough(input, init);
		}
		const method: string = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
		const forEngine: boolean = method !== 'GET' || DEMO_ENGINE_GETS.test(url.pathname);
		const live: LiveDemo | null = engine !== null && engine.active() && forEngine ? await engine.whenReady() : null;
		if (live !== null) {
			const body: string = method === 'GET' ? '' : typeof init?.body === 'string' ? init.body : '';
			try {
				const answer: DemoAnswer | null =
					method === 'GET' ? await answerFromEngine(live, url, responses) : await write(live, url, body);
				if (answer !== null) return reply(answer);
			} catch (error) {
				console.error(`The demo engine could not answer ${method} ${url.pathname}:`, error);
				return reply(failure(error));
			}
		} else if (method !== 'GET') {
			return reply({ status: 403, body: { detail: DEMO_READ_ONLY_MESSAGE } });
		}
		const recording: DemoAnswer | undefined = responses[url.pathname + url.search];
		if (recording === undefined) {
			console.error(`UI demo has no recording for GET ${url.pathname}${url.search}`);
			return reply({ status: 404, body: { detail: `No recording for ${url.pathname}` } });
		}
		return reply(recording);
	};
}
