import { describe, expect, it } from 'vitest';

import { eventStreams, runStartedStreams, terminalRuns } from '../../../../src/lib/demo-readers/_api/run-history';
import type { Streams } from '../../../../src/lib/demo-readers/_helpers/run-assembly';
import type { Query, RunPayload } from '../../../../src/lib/demo-readers/types';

const event: object = {
	invocation_id: 'run-1',
	sequence: '1',
	emitted_at: '2026-01-01 10:00:00.000',
	event_kind: 'run_started',
	step_id: null,
	phase: null,
	payload_json: '{"command":"build","totalStatements":3}'
};

describe('run history API', () => {
	it('given an invocation row when terminal runs are read then the run payload is shaped from it', async () => {
		const query: Query = async <Row>(sql: string): Promise<Row[]> =>
			(sql.includes('count() AS present')
				? [{ present: '1' }]
				: [{ invocation_id: 'run-1', project_identity: 'shop', command: 'build', mode: 'virtual_environment', outcome: 'succeeded', exit_code: 0, started_at: '2026-01-01 10:00:00.000', completed_at: '2026-01-01 10:00:05.000', duration_ms: '5000', selected_node_count: '2', error_message: null, summary_json: '{}', tool_version: '1.0.0' }]) as Row[];

		const runs: Map<string, RunPayload> = await terminalRuns(query, 'shop', { limit: 100, scheduled: false });

		expect(runs.get('run-1')).toMatchObject({ status: 'succeeded', durationMs: 5000, selectedNodeCount: 2, auditSummary: null });
	});

	it('given event rows when streams are read then each event is merged with its payload', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [event] as Row[];

		const streams: Streams = await eventStreams(query, 'shop', { invocationId: 'run-1' }, true);

		expect(streams.get('run-1')).toEqual([
			{ sequence: 1, emittedAt: '2026-01-01 10:00:00.000', event: 'run_started', stepId: null, phase: null, command: 'build', totalStatements: 3 }
		]);
	});

	it('given no events table when start events are read then no streams are returned', async () => {
		const query: Query = async <Row>(): Promise<Row[]> => [event] as Row[];

		expect((await runStartedStreams(query, 'shop', ['run-1'], false)).size).toBe(0);
	});
});
