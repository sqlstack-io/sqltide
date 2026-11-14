import { readDeployment } from '$lib/demo-readers/main/read-deployment';
import { readDeploymentDiff } from '$lib/demo-readers/main/read-deployment-diff';
import { readDeployments } from '$lib/demo-readers/main/read-deployments';
import { readRunEvents } from '$lib/demo-readers/main/read-run-events';
import { readRunStatement } from '$lib/demo-readers/main/read-run-statement';
import { readRuns } from '$lib/demo-readers/main/read-runs';
import { readState } from '$lib/demo-readers/main/read-state';
import type { Query, StateDefinitions, StatePayload } from '$lib/demo-readers/types';
import type { DemoAnswer, LiveDemo } from '../types';

const ok = (body: unknown): DemoAnswer => ({ status: 200, body });
const notFound = (detail: string): DemoAnswer => ({ status: 404, body: { detail } });

/**
 * A GET the engine answers: sqt's plan for this step of the story, the last build, and every page
 * that shows the warehouse, read from the engine's tables as `sqt dev` reads ClickHouse. Null for
 * the rest, which the recording answers (the project's definitions, auth, and settings).
 */
export async function answerFromEngine(
	{ engine, story }: LiveDemo,
	url: URL,
	recorded: Record<string, DemoAnswer>
): Promise<DemoAnswer | null> {
	const query: Query = <Row>(sql: string): Promise<Row[]> => engine.rows<Row>(sql);
	const { database } = engine;
	const path: string[] = url.pathname.split('/').slice(2).map(decodeURIComponent);
	const after: number = Number(url.searchParams.get('after') ?? 0);
	if (url.pathname === '/api/plan') return story.plan(url.searchParams);
	if (url.pathname === '/api/build/current') return ok(story.buildFeed());
	if (url.pathname === '/api/state') {
		return ok(
			await readState(
				query,
				database,
				recorded['/api/state'].body as StatePayload,
				recorded['/api/definitions'].body as StateDefinitions
			)
		);
	}
	if (path[0] === 'deployments') {
		if (path.length === 1) return ok(await readDeployments(query, database));
		if (path.length === 2) {
			const deployment: unknown = await readDeployment(query, database, path[1]);
			return deployment === null ? notFound(`Deployment '${path[1]}' was not found`) : ok(deployment);
		}
		if (path.length === 3 && path[2] === 'diff') {
			return ok(await readDeploymentDiff(query, database, path[1], url.searchParams.get('against')));
		}
	}
	if (path[0] === 'runs') {
		if (path.length === 1) return ok(await readRuns(query, database));
		if (path.length === 3 && path[2] === 'events') return ok(await readRunEvents(query, database, path[1], after));
		if (path.length === 4 && path[2] === 'statements') {
			return ok(await readRunStatement(query, database, path[1], Number(path[3])));
		}
	}
	return null;
}
