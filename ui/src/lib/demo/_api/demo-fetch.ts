import recordings from '../recordings.json';
import type { TableSize } from '../_engine/engine';

/** What every write in the recorded demo returns, and what the UI shows for it. */
export const DEMO_READ_ONLY_MESSAGE: string =
	'This is a read-only UI demo with example data. Run `sqt playground` to try it with ClickHouse on your machine.';

/** What a write other than the staged promotion returns while the real engine runs. */
export const DEMO_ONE_ACTION_MESSAGE: string =
	'This demo runs one action on its engine: promoting the staged deployment. Run `sqt playground` to try the rest with ClickHouse on your machine.';

type Recording = { status: number; body: unknown };

/**
 * The real engine as the API sees it: null while it loads, or in the recorded demo. `promote` runs
 * the promotion's statements, then the API answers from the recording made after it.
 */
export type DemoEngineLink = {
	/** False once the visitor has switched to the recording. */
	active(): boolean;
	ready(): { promote(): Promise<void>; tableSizes(): Promise<Map<string, TableSize>> } | null;
	/** Waits for the engine; rejects when it cannot start. */
	whenReady(): Promise<{ promote(): Promise<void> }>;
	promoted(): boolean;
	markPromoted(): void;
};

function reply(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

type Deployment = {
	deploymentId: string;
	physicalRelationNames: string[];
	rows: number;
	bytes: number;
	models?: {
		stagedRelation: string | null;
		stagedRows: number | null;
		stagedBytes: number | null;
		liveRelation: string | null;
		liveRows: number | null;
	}[];
};

/** A recorded deployment with its row and byte counts read from the engine now. */
function withSizes(deployment: Deployment, sizes: Map<string, TableSize>): Deployment {
	const size = (name: string | null): TableSize | undefined => (name === null ? undefined : sizes.get(name));
	const total = (field: keyof TableSize): number =>
		deployment.physicalRelationNames.reduce((sum, name) => sum + (size(name)?.[field] ?? 0), 0);
	return {
		...deployment,
		rows: total('rows'),
		bytes: total('bytes'),
		...(deployment.models === undefined
			? {}
			: {
					models: deployment.models.map((model) => ({
						...model,
						stagedRows: size(model.stagedRelation)?.rows ?? model.stagedRows,
						stagedBytes: size(model.stagedRelation)?.bytes ?? model.stagedBytes,
						liveRows: size(model.liveRelation)?.rows ?? model.liveRows
					}))
				})
	};
}

/**
 * Serves the UI's `/api` requests from the recorded responses: GETs return what `sqt dev`
 * returned for the same path and query, and writes return the read-only message. A GET with no
 * recording fails loudly, so a UI change that needs a new endpoint is re-recorded.
 *
 * With an engine, deployment sizes come from its tables, and promoting the staged deployment runs
 * the promotion on it; from then on the responses recorded after the promotion come first.
 */
export function installDemoFetch(engine: DemoEngineLink | null = null): void {
	const responses: Record<string, Recording> = recordings.responses;
	const { promotion } = recordings;
	const promotedResponses: Record<string, Recording> = promotion.responses;
	const promotePath: string = `/api/deployments/${promotion.deploymentId}/promote`;
	const passThrough: typeof fetch = globalThis.fetch.bind(globalThis);
	// One promotion per page load, even when the visitor clicks again while it runs.
	let promoting: Promise<void> | null = null;
	globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const url: URL = new URL(
			input instanceof Request ? input.url : String(input),
			globalThis.location.href
		);
		if (url.origin !== globalThis.location.origin || !url.pathname.startsWith('/api/')) {
			return passThrough(input, init);
		}
		const method: string = init?.method ?? (input instanceof Request ? input.method : 'GET');
		if (method.toUpperCase() !== 'GET') {
			if (engine === null || !engine.active()) return reply(403, { detail: DEMO_READ_ONLY_MESSAGE });
			if (url.pathname !== promotePath || engine.promoted()) return reply(403, { detail: DEMO_ONE_ACTION_MESSAGE });
			promoting ??= engine.whenReady().then((ready) => ready.promote());
			try {
				await promoting;
			} catch (error) {
				return reply(500, { detail: `The engine could not promote: ${error instanceof Error ? error.message : String(error)}` });
			}
			engine.markPromoted();
			return reply(promotion.response.status, promotion.response.body);
		}
		const path: string = url.pathname + url.search;
		// Pages only the promotion created, such as its run, answer from after it even on a reload.
		const recording: Recording | undefined = engine?.promoted()
			? (promotedResponses[path] ?? responses[path])
			: (responses[path] ?? promotedResponses[path]);
		if (recording === undefined) {
			console.error(`UI demo has no recording for GET ${url.pathname}${url.search}`);
			return reply(404, { detail: `No recording for ${url.pathname}` });
		}
		const live = engine?.ready() ?? null;
		if (live !== null && recording.status === 200 && /^\/api\/deployments(\/[^/]+)?$/.test(url.pathname)) {
			let sizes: Map<string, TableSize>;
			try {
				sizes = await live.tableSizes();
			} catch (error) {
				console.warn('The demo engine could not read table sizes; showing the recorded ones.', error);
				return reply(recording.status, recording.body);
			}
			const body = recording.body as Deployment & { deployments?: Deployment[] };
			return reply(
				200,
				body.deployments === undefined
					? withSizes(body, sizes)
					: { ...body, deployments: body.deployments.map((deployment) => withSizes(deployment, sizes)) }
			);
		}
		return reply(recording.status, recording.body);
	};
}
