import { beforeEach, describe, expect, it, vi } from 'vitest';

import { answerFromEngine } from '$lib/demo/_api/engine-answers';
import type { DemoAnswer, DemoStory, LiveDemo } from '$lib/demo/types';

const readers = vi.hoisted(() => ({
	readDeployment: vi.fn(),
	readDeploymentDiff: vi.fn(),
	readRunStatement: vi.fn()
}));
vi.mock('$lib/demo-readers/main/read-deployment', () => ({ readDeployment: readers.readDeployment }));
vi.mock('$lib/demo-readers/main/read-deployment-diff', () => ({ readDeploymentDiff: readers.readDeploymentDiff }));
vi.mock('$lib/demo-readers/main/read-run-statement', () => ({ readRunStatement: readers.readRunStatement }));

const ID: string = '20261114T082312Z_0a1b2c';

describe('answers from the engine', () => {
	let live: LiveDemo;

	beforeEach(() => {
		vi.clearAllMocks();
		const story: Partial<DemoStory> = {
			plan: (params: URLSearchParams): DemoAnswer => ({ status: 200, body: { planned: params.getAll('select') } }),
			buildFeed: (): unknown => ({ running: true })
		};
		live = { engine: { database: 'db', rows: vi.fn() }, story } as unknown as LiveDemo;
	});

	const ask: (path: string) => Promise<DemoAnswer | null> = (path) =>
		answerFromEngine(live, new URL(path, 'https://sqltide.io'), {});

	it('given a plan or the build feed when asked then the story answers', async () => {
		expect(await ask('/api/plan?select=order_report&select=large_orders')).toEqual({
			status: 200,
			body: { planned: ['order_report', 'large_orders'] }
		});
		expect(await ask('/api/build/current?after=0')).toEqual({ status: 200, body: { running: true } });
	});

	it('given a deployment the engine does not hold when asked then it is not found, as in sqt dev', async () => {
		readers.readDeployment.mockResolvedValue(null);

		expect(await ask(`/api/deployments/${ID}`)).toEqual({ status: 404, body: { detail: `Deployment '${ID}' was not found` } });
		expect(readers.readDeployment.mock.calls[0].slice(1)).toEqual(['db', ID]);
	});

	it('given a diff against another deployment and a run statement when asked then the reader gets each from the path', async () => {
		readers.readDeploymentDiff.mockResolvedValue({ relations: [] });
		readers.readRunStatement.mockResolvedValue({ found: false });

		await ask(`/api/deployments/${ID}/diff?against=20261114T080000Z_ffffff`);
		await ask('/api/runs/0f1e2d3c-0000-4000-8000-000000000000/statements/12');

		expect(readers.readDeploymentDiff.mock.calls[0].slice(1)).toEqual(['db', ID, '20261114T080000Z_ffffff']);
		expect(readers.readRunStatement.mock.calls[0].slice(1)).toEqual(['db', '0f1e2d3c-0000-4000-8000-000000000000', 12]);
	});

	it('given what the project defines, such as its definitions, when asked then the engine leaves it to the recording', async () => {
		expect(await ask('/api/definitions')).toBeNull();
		expect(await ask('/api/auth/me')).toBeNull();
	});
});
