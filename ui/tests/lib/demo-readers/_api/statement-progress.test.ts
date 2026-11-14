import { describe, expect, it } from 'vitest';

import { statementProgress } from '../../../../src/lib/demo-readers/_api/statement-progress';
import type { JsonObject, Query, RunEvent } from '../../../../src/lib/demo-readers/types';

const started: RunEvent = {
	sequence: 2,
	emittedAt: '2026-01-01 10:00:01.000',
	event: 'statement_started',
	stepId: 'model:orders',
	phase: 'populate',
	statementSequence: 4,
	queryId: 'q-4'
};

describe('statement progress API', () => {
	it('given a running statement in system.processes when progress is read then rates are derived', async () => {
		const query: Query = async <Row>(): Promise<Row[]> =>
			[{ elapsed: 2, read_rows: '100', read_bytes: '800', total_rows_approx: '400', memory_usage: '1024', settings: { b: '2', a: '1' } }] as Row[];

		const progress: JsonObject | null = await statementProgress(query, [started], '2026-01-01 10:00:03.000');

		expect(progress).toMatchObject({ found: true, queryId: 'q-4', statementSequence: 4, readRowsPerSecond: 50, readBytesPerSecond: 400 });
		expect(Object.keys(progress?.settings as object)).toEqual(['a', 'b']);
	});

	it('given a completed statement when progress is read then nothing is running', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [] as Row[];
		const completed: RunEvent = { ...started, sequence: 3, event: 'statement_completed' };

		expect(await statementProgress(query, [started, completed], '2026-01-01 10:00:03.000')).toBeNull();
	});
});
