import { describe, expect, it } from 'vitest';

import type { RunRecord } from '$lib/api/types';
import { runsInScope } from '$lib/run-presentation/main/runs-in-scope';
import { scheduledAuditRuns } from '$lib/run-presentation/main/scheduled-audit-runs';

function run(command: string, mode: string): RunRecord {
	return {
		invocationId: `${command}-${mode}`,
		command,
		displayCommand: null,
		mode,
		status: 'succeeded',
		outcome: 'succeeded',
		exitCode: 0,
		startedAt: '2026-08-21 10:00:00.000',
		completedAt: '2026-08-21 10:01:00.000',
		lastSignalAt: '2026-08-21 10:01:00.000',
		lastSignalAgeSeconds: 0,
		durationMs: 60_000,
		selectedNodeCount: 1,
		errorMessage: null,
		toolVersion: '0.1.0',
		lastActivity: null,
		completedOperationCount: 1,
		totalStatements: 1,
		currentStep: null,
		auditSummary: null
	};
}

describe('run history scopes', () => {
	it('given mixed history when selecting builds then only build operations are shown', () => {
		const runs: RunRecord[] = [run('build', 'direct'), run('audit', 'scheduled'), run('audit', 'direct')];

		expect(runsInScope(runs, 'builds').map((item) => item.invocationId)).toEqual(['build-direct']);
	});

	it('given known operations when selecting scopes then each command family is separate', () => {
		const runs: RunRecord[] = [
			run('audit', 'scheduled'),
			run('test', 'direct'),
			run('deployment promote', 'virtual'),
			run('deployment rollback', 'virtual_environment'),
			run('destroy pipelines', 'destructive'),
			run('reset target', 'destructive'),
			run('janitor', 'virtual')
		];

		expect(runsInScope(runs, 'audits').map((item) => item.invocationId)).toEqual([
			'audit-scheduled'
		]);
		expect(runsInScope(runs, 'tests').map((item) => item.invocationId)).toEqual(['test-direct']);
		expect(runsInScope(runs, 'deployments').map((item) => item.invocationId)).toEqual([
			'deployment promote-virtual',
			'deployment rollback-virtual_environment'
		]);
		expect(runsInScope(runs, 'destruction').map((item) => item.invocationId)).toEqual([
			'destroy pipelines-destructive',
			'reset target-destructive'
		]);
		expect(runsInScope(runs, 'maintenance').map((item) => item.invocationId)).toEqual([
			'janitor-virtual'
		]);
	});

	it('given unknown historical commands when selecting other then known commands are excluded', () => {
		const runs: RunRecord[] = [run('build', 'direct'), run('audit', 'direct'), run('mystery', 'direct')];

		expect(runsInScope(runs, 'other').map((item) => item.invocationId)).toEqual(['mystery-direct']);
	});

	it('given scheduled and manual audits when reading cycles then only scheduled audits are returned', () => {
		const scheduled: RunRecord = run('audit', 'scheduled');
		const manual: RunRecord = run('audit', 'direct');

		expect(scheduledAuditRuns([manual, scheduled])).toEqual([scheduled]);
	});
});
