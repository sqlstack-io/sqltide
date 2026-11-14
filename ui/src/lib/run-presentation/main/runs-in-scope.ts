import type { RunRecord } from '$lib/api/types';

export type RunScope =
	| 'builds'
	| 'audits'
	| 'tests'
	| 'deployments'
	| 'destruction'
	| 'maintenance'
	| 'other'
	| 'all';

const COMMANDS_BY_SCOPE: Record<Exclude<RunScope, 'all' | 'other'>, Set<string>> = {
	builds: new Set(['build']),
	audits: new Set(['audit']),
	tests: new Set(['test']),
	deployments: new Set(['deployment promote', 'deployment rollback']),
	destruction: new Set(['destroy pipelines', 'reset target']),
	maintenance: new Set(['janitor'])
};

const KNOWN_COMMANDS: Set<string> = new Set(
	Object.values(COMMANDS_BY_SCOPE).flatMap((commands: Set<string>): string[] => [...commands])
);

export function runsInScope(runs: RunRecord[], scope: RunScope): RunRecord[] {
	if (scope === 'all') return runs;
	if (scope === 'other') return runs.filter((run) => !KNOWN_COMMANDS.has(run.command));
	return runs.filter((run) => COMMANDS_BY_SCOPE[scope].has(run.command));
}
