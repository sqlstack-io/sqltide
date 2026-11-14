import { base } from '$app/paths';
import type { RunEvent } from '$lib/api/types';
import type {
	RunActivityPresentation,
	RunPresentationInput
} from '$lib/run-presentation/types';

export function buildRunActivity(input: RunPresentationInput): RunActivityPresentation {
	const startedEvent: RunEvent | undefined = input.events.find(
		(event: RunEvent) => event.event === 'run_started'
	);
	const auditRun: boolean = (startedEvent?.command ?? input.record?.command) === 'audit';
	const completedStatements: RunEvent[] = input.events.filter((event: RunEvent) =>
		auditRun ? event.event === 'audit_completed' : event.event === 'statement_completed'
	);
	const recordedTotal: number = startedEvent?.totalStatements ?? 0;
	const totalStatements: number | null = recordedTotal > 0 ? recordedTotal : null;
	const statementSummary: string | null =
		totalStatements !== null
			? `${completedStatements.length}/${totalStatements}`
			: !input.running && completedStatements.length > 0
				? `${completedStatements.length}`
				: null;
	const displayCommand: string = input.commandLine.startsWith('sqt ')
		? input.commandLine
		: `sqt ${input.commandLine}`;
	return {
		startedEvent,
		completedStatements,
		totalStatements,
		statementSummary,
		displayCommand,
		retryHref: buildRetryHref(input.running, input.commandLine, startedEvent),
		durationSeconds: calculateDurationSeconds(input, startedEvent)
	};
}

function buildRetryHref(
	running: boolean,
	commandLine: string,
	startedEvent: RunEvent | undefined
): string | null {
	if (running || !(commandLine === 'build' || commandLine.startsWith('sqt build'))) return null;
	const params: URLSearchParams = new URLSearchParams();
	for (const selector of startedEvent?.selectors ?? []) params.append('select', selector);
	if (startedEvent?.startTime) params.set('start', startedEvent.startTime);
	const query: string = params.toString();
	return query ? `${base}/plan?${query}` : `${base}/plan`;
}

function calculateDurationSeconds(
	input: RunPresentationInput,
	startedEvent: RunEvent | undefined
): number | null {
	if (
		input.record !== null &&
		input.record.status !== 'running' &&
		input.record.status !== 'unresponsive'
	) {
		return input.record.durationMs / 1000;
	}
	if (startedEvent === undefined) return null;
	const start: number = Date.parse(`${startedEvent.emittedAt.replace(' ', 'T')}Z`);
	const last: RunEvent | undefined = input.events[input.events.length - 1];
	const end: number =
		input.running || last === undefined
			? input.nowMs
			: Date.parse(`${last.emittedAt.replace(' ', 'T')}Z`);
	return Math.max((end - start) / 1000, 0);
}
