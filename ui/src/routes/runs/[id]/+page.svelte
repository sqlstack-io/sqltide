<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import RotateCcwIcon from '@lucide/svelte/icons/rotate-ccw';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import ErrorView from '$lib/presentation/components/error-view.svelte';
	import LineageCanvas from '$lib/presentation/components/lineage/lineage-canvas.svelte';
	import RunTimeline from '$lib/presentation/components/run-timeline.svelte';
	import StatementProgress from '$lib/presentation/components/statement-progress.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import type { Project } from '$lib/domain/types';
	import { can } from '$lib/auth/main/can';
	import { canAnyPipeline } from '$lib/auth/main/can-any-pipeline';
	import { buildRunPresentation } from '$lib/run-presentation/main/build-run-presentation';
	import { labelRunStepId } from '$lib/run-presentation/main/label-run-step-id';
	import { createRunDetail } from '$lib/run-presentation/main/create-run-detail';
	import type { RunPresentation } from '$lib/run-presentation/types';
	import DestructionRecoveryAction from './destruction-recovery-action.svelte';

	const project: Project = getProject();
	const cancelAllowed = $derived(canAnyPipeline('build.cancel'));
	const killAllowed = $derived(can('build.kill'));
	const invocationId = $derived(page.params.id ?? '');
	const detail: ReturnType<typeof createRunDetail> = createRunDetail(
		async (nextInvocationId: string): Promise<void> => {
			await goto(`${base}/runs/${nextInvocationId}?live=1`, { replaceState: true, noScroll: true });
		}
	);
	$effect(() => {
		const id: string = invocationId;
		const live: boolean = page.url.searchParams.get('live') === '1';
		detail.start(id, live);
		return (): void => detail.stop();
	});

	const presentation = $derived<RunPresentation>(
		buildRunPresentation({
			project,
			events: detail.view.events,
			running: detail.view.running,
			status: detail.view.status,
			commandLine: detail.view.commandLine,
			record: detail.view.record,
			nowMs: Date.now()
		})
	);
	const {
		running,
		status,
		stderr,
		ownedRunning,
		forceAvailable,
		signalling,
		lastSignalAgeSeconds,
		record,
		loadError,
		cancellationError,
		pollError,
		notFound,
		initialLoading,
		events
	} = $derived(detail.view);
	const {
		startedEvent,
		completedStatements,
		totalStatements,
		statementSummary,
		displayCommand,
		retryHref,
		outcome,
		outcomeColor,
		runGraph,
		mutedIds,
		notes,
		recordedScopeCount,
		missingScopeCount,
		timeline,
		eventLabels,
		durationSeconds
	} = $derived(presentation);
	const activeStatementLabel = $derived.by((): string => {
		const progress: typeof detail.view.statementProgress = detail.view.statementProgress;
		if (progress === null) return '';
		const event: (typeof timeline)[number] | undefined = timeline.find(
			(item) => item.event === 'statement_started' && item.statementSequence === progress.statementSequence
		);
		return (
			(event === undefined ? null : eventLabels.get(event.sequence)) ??
			(progress.stepId === null ? 'Warehouse statement' : labelRunStepId(progress.stepId))
		);
	});

	async function openDestructionPlan(planId: string): Promise<void> {
		await goto(`${base}/destruction/plans/${encodeURIComponent(planId)}`);
	}
</script>

<AppTopbar title="Run" breadcrumb={`${project.name} / runs / ${invocationId.slice(0, 8)}`} />

<div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
	{#if notFound}
		<div class="grid min-h-[320px] flex-1 place-items-center p-[18px]">
			<div class="max-w-md rounded-[4px] border border-border p-5 text-center">
				<div class="font-display text-[16px] font-semibold">Run not found</div>
				<p class="text-muted-foreground pt-2 font-mono text-[11.5px]">
					No durable run or active local process matches <code class="code">{invocationId}</code>.
				</p>
				<a href="{base}/runs" class="text-primary mt-3 inline-block font-mono text-[11px] hover:underline">
					Back to runs
				</a>
			</div>
		</div>
	{:else if initialLoading}
		<div class="flex min-h-[520px] flex-1 flex-col" aria-label="Loading run details">
			<div class="flex h-[53px] shrink-0 items-center gap-3 border-b border-border px-[18px]">
				<div class="h-5 w-16 animate-pulse rounded bg-[var(--sb-inset)]"></div>
				<div class="h-6 w-24 animate-pulse rounded-full bg-[var(--sb-inset)]"></div>
				<div class="h-7 min-w-0 flex-1 animate-pulse rounded bg-[var(--sb-inset)]"></div>
			</div>
			<div class="grid h-[380px] shrink-0 place-items-center border-b border-border">
				<span class="text-muted-foreground font-mono text-[11px]">loading run timeline…</span>
			</div>
			<div class="m-[18px] h-24 animate-pulse rounded-[4px] border border-border bg-[var(--sb-inset)]/40"></div>
		</div>
	{:else}
	<!-- header -->
	<div class="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-[18px] py-3">
		<a href="{base}/runs" class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 font-mono text-[11px]">
			<ArrowLeftIcon size={12} /> runs
		</a>
		<span
			class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[10.5px]"
			style:border-color="color-mix(in srgb, {outcomeColor} 40%, var(--border))"
			style:color={outcomeColor}
		>
			<span
				class="h-1.5 w-1.5 rounded-full {outcome === 'running' ? 'animate-pulse' : ''}"
				style:background={outcomeColor}
			></span>
			{outcome}
		</span>
		<code class="code max-w-full break-all text-[11px]" aria-label="Run ID" title={invocationId}
			>{invocationId}</code
		>
		<code
			class="bg-[var(--sb-inset)] min-w-0 flex-1 truncate rounded-[4px] border border-border px-2.5 py-1 font-mono text-[11px]"
			>$ {displayCommand}</code
		>
		<span class="text-muted-foreground shrink-0 font-mono text-[11px]">
			{#if durationSeconds !== null}{formatDuration(durationSeconds)}{/if}
			{#if statementSummary !== null}
				· {statementSummary} {record?.command === 'audit' || startedEvent?.command === 'audit' ? 'audits' : 'statements'}
			{/if}
		</span>
		{#if retryHref}
			<a
				href={retryHref}
				class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded border border-border px-2.5 py-1 font-mono text-[10.5px]"
			>
				<RotateCcwIcon size={11} /> Open in Plan
			</a>
		{/if}
		<DestructionRecoveryAction
			{invocationId}
			{outcome}
			command={startedEvent?.command ?? record?.command ?? null}
			mode={startedEvent?.mode ?? record?.mode ?? null}
			onPlanCreated={openDestructionPlan}
		/>
		{#if ownedRunning && running && status !== 'cancelling' && status !== 'cancellation_failed'}
			<button
				class="rounded border border-border px-2.5 py-1 font-mono text-[10.5px] text-[var(--sb-warning)]"
				disabled={signalling || !cancelAllowed}
				title={cancelAllowed ? undefined : 'Requires the build.cancel permission'}
				onclick={() => void detail.requestCancel()}>Cancel</button
			>
		{/if}
		{#if status === 'cancelling'}
			<span class="font-mono text-[10.5px] text-[var(--sb-warning)]">Cancelling warehouse query…</span>
		{/if}
		{#if running && !ownedRunning}
			<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">
				This server does not own the process and cannot cancel it.
			</span>
		{/if}
		{#if forceAvailable}
			<button
				class="rounded border px-2.5 py-1 font-mono text-[10.5px]"
				style:color="var(--sb-error)"
				disabled={signalling || !killAllowed}
				title={killAllowed ? undefined : 'Requires the target-scoped build.kill permission'}
				onclick={() => void detail.requestKill()}>Force kill</button
			>
		{/if}
	</div>
	{#if status === 'unresponsive' || status === 'presumed_failed'}
		<div class="border-b border-border px-[18px] py-2 font-mono text-[11px]" style:color="var(--sb-warning)">
			{status === 'presumed_failed' ? 'Presumed failed' : 'Unresponsive'} — no signal for
			{lastSignalAgeSeconds ?? 0}s. Last activity: {record?.lastActivity ?? 'unknown'}.
			The process may recover; rerunning the build is safe once it is presumed failed.
			{#if status === 'presumed_failed' && retryHref}
				<a href={retryHref} class="pl-2 underline">Rerun from Plan</a>
			{/if}
		</div>
	{/if}
	{#if status === 'cancellation_failed'}
		<div class="border-b border-border px-[18px] py-2 font-mono text-[11px] text-[var(--sb-error)]" role="alert">
			Cancellation failed — {cancellationError ?? record?.errorMessage ?? 'the warehouse did not confirm query termination'}. Check the query ID below before retrying or force-killing the worker.
		</div>
	{/if}

	{#if loadError}
		<div class="border-b border-border px-[18px] py-2">
			<p class="font-mono text-[12px]" style:color="var(--sb-error)">{loadError}</p>
		</div>
	{/if}
	{#if pollError}
		<div class="border-b border-border px-[18px] py-2 font-mono text-[11px]" style:color="var(--sb-warning)">
			Observability temporarily unavailable: {pollError}. Retrying…
		</div>
	{/if}
		<!-- progress -->
		{#if totalStatements !== null && totalStatements > 0}
			<div class="shrink-0 px-[18px] pt-3">
				<div class="h-[4px] w-full overflow-hidden rounded-full bg-[var(--sb-inset)]">
					<div
						class="h-full rounded-full transition-all"
						style:width="{Math.min((completedStatements.length / totalStatements) * 100, 100)}%"
						style:background={outcomeColor}
					></div>
				</div>
			</div>
		{/if}
		{#if running && detail.view.statementProgress !== null}
			<StatementProgress
				progress={detail.view.statementProgress}
				label={activeStatementLabel}
				{totalStatements}
				workerSignalAgeSeconds={lastSignalAgeSeconds}
			/>
		{/if}

		<!-- the pipeline, growing as replays land -->
		<div class="h-[380px] shrink-0 border-b border-border">
			{#if runGraph.nodes.length > 0}
				<div class="flex h-full flex-col">
					{#if missingScopeCount > 0}
						<div class="border-b border-border px-3 py-1.5 font-mono text-[10.5px] text-[var(--sb-warning)]">
							{missingScopeCount} recorded scope {missingScopeCount === 1 ? 'node is' : 'nodes are'} no longer present in the current project.
						</div>
					{/if}
					<div class="min-h-0 flex-1">
						<LineageCanvas
							{project}
							graph={runGraph}
							{mutedIds}
							{notes}
							groupMode="none"
							compactNodes
							embedded
						/>
					</div>
				</div>
			{:else if ownedRunning && events.length === 0}
				<div
					class="text-muted-foreground grid h-full place-items-center px-6 text-center font-mono text-[11px]"
					role="status"
					aria-label="Loading run timeline"
				>
					<span class="flex items-center gap-2.5">
						<RotateCcwIcon size={13} class="animate-spin" /> loading run timeline…
					</span>
				</div>
			{:else if startedEvent === undefined && ownedRunning}
				<div class="text-muted-foreground grid h-full place-items-center px-6 text-center font-mono text-[11px]">
					Compiling project and inspecting warehouse…
				</div>
			{:else if recordedScopeCount > 0}
				<div class="text-muted-foreground grid h-full place-items-center px-6 text-center font-mono text-[11px]">
					The recorded scope no longer exists in the current project.
				</div>
			{:else}
				<div class="text-muted-foreground grid h-full place-items-center px-6 text-center font-mono text-[11px]">
					Scope was not recorded for this run. Project-wide lineage is intentionally not shown.
				</div>
			{/if}
		</div>

		{#if stderr && outcome === 'failed'}
			<div class="shrink-0 px-[18px] pt-3">
				<ErrorView text={stderr} maxHeight="240px" />
			</div>
		{/if}

		<!-- event timeline -->
		<RunTimeline
			{invocationId}
			{timeline}
			{running}
			{ownedRunning}
			{outcomeColor}
			{eventLabels}
			audits={project.audits}
		/>
	{/if}
</div>
