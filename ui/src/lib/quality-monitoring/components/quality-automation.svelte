<script lang="ts">
	import { base } from '$app/paths';
	import { fetchRuns } from '$lib/api/main/runs/fetch-runs';
	import type { RunRecord } from '$lib/api/types';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import { scheduledAuditRuns } from '$lib/run-presentation/main/scheduled-audit-runs';

	type Props = {
		view: 'current' | 'history';
		capturedAt: string;
	};

	const { view, capturedAt }: Props = $props();
	let cycleRuns = $state<RunRecord[] | null>(null);
	let cycleLoadError = $state<string | null>(null);
	let loadingCycles = false;
	const cycles = $derived(scheduledAuditRuns(cycleRuns ?? []));

	async function loadCycles(): Promise<void> {
		if (cycleRuns !== null || loadingCycles) return;
		loadingCycles = true;
		try {
			cycleRuns = await fetchRuns();
			cycleLoadError = null;
		} catch (caught) {
			cycleLoadError = caught instanceof Error ? caught.message : String(caught);
		} finally {
			loadingCycles = false;
		}
	}

	$effect(() => {
		if (view === 'history') void loadCycles();
	});

	function cycleSummary(run: RunRecord): string {
		const summary: RunRecord['auditSummary'] = run.auditSummary;
		if (!summary) return `${run.completedOperationCount ?? 0}/${run.selectedNodeCount} completed`;
		const parts: string[] = [`${summary.passed} passed`];
		if (summary.warning) parts.push(`${summary.warning} warning`);
		if (summary.failed) parts.push(`${summary.failed} failed`);
		if (summary.error) parts.push(`${summary.error} errors`);
		return parts.join(' · ');
	}
</script>

{#if view === 'history'}
	<div>
		<div class="pb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--sb-text-faint)]">
			Scheduled audit cycles
		</div>
		{#if cycleLoadError}
			<div class="rounded-[4px] border border-border p-3 font-mono text-[11px]" style:color="var(--sb-error)">
				{cycleLoadError}
			</div>
		{:else if cycleRuns === null}
			<div class="text-muted-foreground font-mono text-[11px]">Loading cycle history…</div>
		{:else if cycles.length === 0}
			<div class="rounded-[4px] border border-border p-3 text-[12px] text-muted-foreground">
				No scheduled audit cycles have been recorded for this target.
			</div>
		{:else}
			<div class="overflow-hidden rounded-[4px] border border-border">
				<table class="sb-list w-full text-left md:table-fixed">
					<thead class="sticky top-0 z-10">
						<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.12em]">
							<th class="w-[120px] px-3 py-2.5 font-medium">Cycle</th>
							<th class="w-[120px] px-3 py-2.5 font-medium">Outcome</th>
							<th class="hidden px-3 py-2.5 font-medium md:table-cell">Audit results</th>
							<th class="hidden w-[152px] px-3 py-2.5 font-medium whitespace-nowrap lg:table-cell">Started</th>
							<th class="hidden w-[90px] px-3 py-2.5 text-right font-medium whitespace-nowrap xl:table-cell">Duration</th>
						</tr>
					</thead>
					<tbody>
						{#each cycles as cycle (cycle.invocationId)}
							<tr>
								<td class="px-3 py-2.5">
									<a href="{base}/runs/{cycle.invocationId}" class="code text-primary text-[12px] hover:underline">
										{cycle.invocationId.slice(0, 8)}
									</a>
									<div class="pt-1 font-mono text-[10px] text-[var(--sb-text-faint)]">{cycle.selectedNodeCount} audits</div>
								</td>
								<td class="px-3 py-2.5 align-top">
									<span style:color={cycle.status === 'succeeded' ? 'var(--sb-success)' : cycle.status === 'running' ? 'var(--sb-secondary)' : 'var(--sb-error)'} class="font-mono text-[11px]">
										{cycle.status === 'succeeded' ? 'Complete' : cycle.status === 'running' ? 'Running' : cycle.status.replace('_', ' ')}
									</span>
								</td>
								<td class="hidden px-3 py-2.5 align-top font-mono text-[11px] md:table-cell">
									{cycleSummary(cycle)}
								</td>
								<td class="hidden px-3 py-2.5 align-top lg:table-cell">
									<div class="text-[12px]">{formatTimestamp(cycle.startedAt)}</div>
									<div class="font-mono text-[10px] text-[var(--sb-text-faint)]">{formatAgo(cycle.startedAt, capturedAt)}</div>
								</td>
								<td class="hidden px-3 py-2.5 text-right align-top font-mono text-[11px] text-muted-foreground xl:table-cell">
									{formatDuration(cycle.durationMs / 1000)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
{/if}
