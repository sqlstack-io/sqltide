<script lang="ts">
	import { base } from '$app/paths';
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';
	import { fetchRunStatement } from '$lib/api/main/build/fetch-run-statement';
	import type { RunEvent, RunStatement } from '$lib/api/types';
	import type { Audit } from '$lib/domain/types';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import ErrorPreview from '$lib/presentation/components/error-preview.svelte';
	import SqlBlock from '$lib/presentation/components/sql-block.svelte';
	import { labelRunPhase } from '$lib/run-presentation/main/label-run-phase';

	interface RunTimelineProps {
		invocationId: string;
		timeline: RunEvent[];
		running: boolean;
		ownedRunning: boolean;
		outcomeColor: string;
		eventLabels: Map<number, string>;
		audits: Audit[];
	}

	const { invocationId, timeline, running, ownedRunning, outcomeColor, eventLabels, audits }: RunTimelineProps =
		$props();

	let expandedEventSequence = $state<number | null>(null);
	let runStatements = $state<Record<number, RunStatement>>({});
	let statementLoading = $state<Set<number>>(new Set());
	let statementErrors = $state<Record<number, string>>({});

	function expandable(event: RunEvent): boolean {
		return event.statementSequence !== undefined || event.event === 'audit_completed';
	}

	function auditFor(event: RunEvent): Audit | undefined {
		return event.event === 'audit_completed'
			? audits.find((audit: Audit) => audit.name === event.stepId)
			: undefined;
	}

	async function toggleEvent(event: RunEvent): Promise<void> {
		if (!expandable(event)) return;
		const sequence: number | undefined = event.statementSequence;
		if (expandedEventSequence === event.sequence) {
			expandedEventSequence = null;
			return;
		}
		expandedEventSequence = event.sequence;
		if (sequence === undefined) return;
		if (runStatements[sequence] || statementLoading.has(sequence)) return;
		const requestedInvocationId: string = invocationId;
		statementLoading = new Set(statementLoading).add(sequence);
		try {
			const statement: RunStatement = await fetchRunStatement(requestedInvocationId, sequence);
			if (invocationId !== requestedInvocationId) return;
			runStatements[sequence] = statement;
			runStatements = { ...runStatements };
		} catch (error) {
			if (invocationId !== requestedInvocationId) return;
			statementErrors[sequence] = String(error);
			statementErrors = { ...statementErrors };
		} finally {
			if (invocationId !== requestedInvocationId) return;
			const nextLoading: Set<number> = new Set(statementLoading);
			nextLoading.delete(sequence);
			statementLoading = nextLoading;
		}
	}

	$effect(() => {
		void invocationId;
		expandedEventSequence = null;
		runStatements = {};
		statementLoading = new Set();
		statementErrors = {};
	});
</script>

<div class="min-h-0 flex-1 p-[18px]">
	<div class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px] uppercase tracking-[0.14em]">
		Events {#if running}<span class="text-[var(--sb-secondary)]">· live</span>{/if}
	</div>
	<div class="overflow-hidden rounded-[4px] border border-border">
		{#if timeline.length === 0 && ownedRunning}
			<div class="flex items-center gap-3 px-3 py-1.5">
				<span class="text-[var(--sb-text-faint)] w-[86px] shrink-0 font-mono text-[10.5px]">now</span>
				<span class="w-[92px] shrink-0"><span class="sb-tag code">startup</span></span>
				<span class="code min-w-0 flex-1 text-[11.5px]">Compile project and inspect warehouse</span>
			</div>
		{/if}
		{#each timeline as event (event.sequence)}
			<div class="border-b border-[var(--border-subtle)] last:border-b-0">
				<button
					type="button"
					data-statement-sequence={event.statementSequence}
					data-audit-name={event.event === 'audit_completed' ? event.stepId : undefined}
					aria-expanded={!expandable(event)
						? undefined
						: expandedEventSequence === event.sequence}
					title={!expandable(event)
						? undefined
						: event.statementSequence === undefined
							? 'Show audit details'
							: 'Show executed SQL'}
					class="flex w-full items-center gap-3 px-3 py-1.5 text-left {event.statementSequence ===
					undefined && event.event !== 'audit_completed'
						? 'cursor-default'
						: 'hover:bg-[var(--sb-hover)]'}"
					onclick={() => void toggleEvent(event)}
				>
					<span class="text-[var(--sb-text-faint)] w-[86px] shrink-0 font-mono text-[10.5px]"
						>{formatTimestamp(event.emittedAt).slice(11)}</span
					>
					<span class="w-[92px] shrink-0">
						{#if event.phase}
							<span class="sb-tag code">{labelRunPhase(event.phase)}</span>
						{:else}
							<span
								class="sb-tag code"
								style:color={event.event === 'run_completed'
									? outcomeColor
									: 'var(--sb-secondary)'}>{event.event.replace('_', ' ')}</span
							>
						{/if}
					</span>
					<span class="code min-w-0 flex-1 truncate text-[11.5px]" title={event.stepId ?? undefined}
						>{eventLabels.get(event.sequence)}</span
					>
					{#if event.writtenRows !== null && event.writtenRows !== undefined}
						<span class="shrink-0 font-mono text-[10.5px]" style:color="var(--sb-secondary)"
							>{formatCompact(event.writtenRows)} rows</span
						>
					{/if}
					{#if event.elapsedMs !== undefined}
						<span
							class="text-[var(--sb-text-faint)] w-[64px] shrink-0 text-right font-mono text-[10.5px]"
							>{event.elapsedMs} ms</span
						>
					{/if}
					{#if expandable(event)}
						<ChevronDownIcon
							size={13}
							class="text-muted-foreground shrink-0 transition-transform {expandedEventSequence ===
							event.sequence
								? 'rotate-180'
								: ''}"
						/>
					{/if}
				</button>
				{#if event.errorMessage}
					<div class="border-t border-[var(--border-subtle)] bg-[var(--sb-surface-low)] px-3 py-1.5">
						<ErrorPreview
							text={event.errorMessage}
							title={event.event === 'audit_completed' ? 'Audit error' : 'Statement error'}
							subtitle={event.stepId ?? undefined}
							class="w-full"
						/>
					</div>
				{/if}
				{#if event.statementSequence !== undefined && expandedEventSequence === event.sequence}
					<div class="border-t border-[var(--border-subtle)] bg-[var(--sb-surface-low)] p-3">
						{#if statementLoading.has(event.statementSequence)}
							<div class="text-muted-foreground font-mono text-[11px]">loading SQL…</div>
						{:else if statementErrors[event.statementSequence]}
							<div class="font-mono text-[11px]" style:color="var(--sb-error)">
								{statementErrors[event.statementSequence]}
							</div>
						{:else if runStatements[event.statementSequence]?.found && runStatements[event.statementSequence]?.sql}
							<SqlBlock
								artifacts={[
									{ label: 'executed', code: runStatements[event.statementSequence].sql ?? null }
								]}
								maxHeight="420px"
								caption={`statement ${event.statementSequence} · ${event.stepId ?? ''}`}
							/>
						{:else}
							<div class="text-muted-foreground font-mono text-[11px]">
								SQL was not recorded for this run.
							</div>
						{/if}
					</div>
				{:else if event.event === 'audit_completed' && expandedEventSequence === event.sequence}
					{@const audit = auditFor(event)}
					<div class="flex flex-col gap-3 border-t border-[var(--border-subtle)] bg-[var(--sb-surface-low)] p-3">
						<div class="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10.5px]">
							<span style:color={event.status === 'passed' ? 'var(--sb-success)' : event.status === 'warning' ? 'var(--sb-warning)' : 'var(--sb-error)'}>
								{event.status ?? 'unknown'}
							</span>
							<span class="text-[var(--sb-text-faint)]">
								{formatCompact(event.failureCount ?? 0)} failing {event.failureCount === 1 ? 'row' : 'rows'}
							</span>
							{#if audit && event.status !== 'passed'}
								<span class="text-[var(--sb-text-faint)]">severity {audit.severity}</span>
							{/if}
						</div>
						{#if audit?.description}
							<p class="text-muted-foreground text-[12px] leading-relaxed">{audit.description}</p>
						{/if}
						{#if audit?.referencedModels.length}
							<div class="flex flex-wrap items-center gap-1.5 font-mono text-[10.5px]">
								<span class="text-[var(--sb-text-faint)]">models</span>
								{#each audit.referencedModels as model (model)}
									<a href="{base}/catalog/{model}" class="text-primary hover:underline">{model}</a>
								{/each}
							</div>
						{/if}
						{#if audit}
							<SqlBlock
								artifacts={[{ label: 'Current audit SQL', code: audit.sql }]}
								maxHeight="320px"
								caption={`${audit.file} · current project definition`}
							/>
						{:else}
							<div class="text-muted-foreground font-mono text-[11px]">
								This audit is not present in the current project definition.
							</div>
						{/if}
					</div>
				{/if}
			</div>
		{/each}
	</div>
</div>
