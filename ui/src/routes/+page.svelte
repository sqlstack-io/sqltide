<script lang="ts">
	import GitCompareIcon from '@lucide/svelte/icons/git-compare';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import Sparkline from '$lib/presentation/components/sparkline.svelte';
	import StatusPill from '$lib/presentation/components/status-pill.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { auditCounts } from '$lib/domain/main/quality/audit-counts';
	import { testCounts } from '$lib/domain/main/quality/test-counts';
	import { projectHealthSummary } from '$lib/domain/main/summaries/project-health-summary';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatRate } from '$lib/formatting/main/format-rate';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import type { Project } from '$lib/domain/types';
	import WarehouseHealthSummary from '$lib/warehouse-health/components/warehouse-health-summary.svelte';

	const project: Project = getProject();

	// Not "did last night's run succeed" — there are no runs. The question a
	// streaming operator actually has is: is data moving, and does the warehouse
	// still match my code?
	const healthSummary = $derived(projectHealthSummary(project));
	const freshness = $derived(healthSummary.freshness);
	const ingest = $derived(healthSummary.ingest);
	const deadLetters = $derived(healthSummary.deadLetters);
	const ingestLabel = $derived(
		ingest.state === 'healthy'
			? 'Healthy'
			: ingest.state === 'behind'
				? 'Behind'
				: ingest.state === 'error'
					? 'Error'
					: ingest.state === 'partial'
						? 'Partial'
						: 'No Kafka'
	);
	const ingestTone = $derived(
		ingest.state === 'healthy' || ingest.state === 'no_kafka'
			? 'var(--sb-success)'
			: ingest.state === 'behind' || ingest.state === 'partial'
				? 'var(--sb-warning)'
				: 'var(--sb-error)'
	);
	const drifted = $derived(healthSummary.drifted);
	const visibleDrifted = $derived(drifted.slice(0, 24));
	const hiddenDriftedCount = $derived(drifted.length - visibleDrifted.length);
	const audits = $derived(auditCounts(project.audits));
	const tests = $derived(testCounts(project.tests));

	// Overview must survive a project with 20+ sources, so the list is sorted
	// oldest-arrival-first and capped — you scan for what is NOT moving.
	const SOURCE_VISIBLE_LIMIT: number = 6;
	const PARTITION_TICK_LIMIT: number = 12;

	let showAllSources = $state<boolean>(false);

	const sortedSources = $derived(
		[...project.sources].sort(
			(a, b) => (b.live.lastArrivalSeconds ?? -1) - (a.live.lastArrivalSeconds ?? -1)
		)
	);
	const visibleSources = $derived(
		showAllSources ? sortedSources : sortedSources.slice(0, SOURCE_VISIBLE_LIMIT)
	);
	const hiddenSourceCount = $derived(sortedSources.length - visibleSources.length);
	const totalThroughput = $derived(
		project.sources.reduce((sum, source) => sum + source.live.rowsPerSecond, 0)
	);
	const monitoredSources = $derived(
		project.sources.filter((source) => source.live.freshness !== null).length
	);

	const worstAudit = $derived(
		project.audits
			.filter((audit) => audit.result && !audit.result.passed)
			.sort((a, b) => (b.result?.failingRowCount ?? 0) - (a.result?.failingRowCount ?? 0))[0]
	);
</script>

<AppTopbar title="Overview" />

<div class="min-h-0 flex-1 overflow-y-auto">
	<!-- ── standing conditions banner ──────────────────────────────────────── -->
	{#if drifted.length}
		<div class="flex flex-col divide-y divide-[var(--border-subtle)] border-b border-border">
			{#if drifted.length}
				<div
					class="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-[18px] py-2.5"
					style:background="color-mix(in srgb, var(--sb-stale) 8%, transparent)"
				>
					<GitCompareIcon size={14} color="var(--sb-stale)" class="shrink-0" />
					<span class="text-[12.5px]">
						<strong class="font-medium">{drifted.length}</strong>
						{drifted.length === 1 ? 'model changed' : 'models changed'} since the last applied build
					</span>
					<span class="text-muted-foreground font-mono text-[11px]">
						{visibleDrifted.map((model) => model.name).join(' · ')}
					</span>
					{#if hiddenDriftedCount > 0}
						<span class="text-muted-foreground shrink-0 font-mono text-[11px]">
							+{hiddenDriftedCount} more
						</span>
					{/if}
					<a
						href="/lineage?status=drift"
						class="text-primary shrink-0 font-mono text-[11px] hover:underline">Review all</a
					>
					<a
						href="/plan?changed=1&include_missing_upstream=1"
						class="text-primary ml-auto shrink-0 font-mono text-[11px] hover:underline"
						>Plan changed + prerequisites</a
					>
				</div>
			{/if}
		</div>
	{/if}

	<div class="flex flex-col gap-5 p-[18px]">
		<!-- ── ingest ──────────────────────────────────────────────────────── -->
		<div>
			<div
				class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Ingest
				<span class="ml-auto flex flex-wrap items-center justify-end gap-x-2 normal-case tracking-normal">
					<strong class="font-medium uppercase" style:color={ingestTone}>{ingestLabel}</strong>
					<span>{formatRate(totalThroughput)}</span>
					{#if ingest.polling !== null}<span>{ingest.polling}/{ingest.materialized} live polling</span>{/if}
					{#if ingest.notBuilt}<span>{ingest.notBuilt} not built</span>{/if}
					{#if ingest.exceptions !== null}<span>{ingest.exceptions} errors</span>{/if}
					{#if ingest.stuckConsumers.length}<span style:color="var(--sb-error)">{ingest.stuckConsumers.length} stuck</span>{/if}
					<span>{ingest.behind} behind</span>
					{#if ingest.lagUnavailable}<span>{ingest.lagUnavailable} lag unavailable</span>{/if}
					{#if deadLetters.state === 'unavailable'}
						<span>dead letters unavailable</span>
					{:else if deadLetters.state === 'present'}
						<span>{deadLetters.total} dead letters 24h</span>
					{/if}
				</span>
			</div>
			{#if deadLetters.state === 'present'}
				<ul class="mb-2 space-y-1 font-mono text-[10px]" data-testid="dead-letters">
					{#each deadLetters.models as model (model.modelName)}
						<li class="flex flex-col gap-0.5">
							<span>
								{model.modelName}: {model.rowCount} in 24h, latest failed
								{model.latestFailedColumns.join(', ')}
							</span>
							<span class="break-all text-[var(--sb-text-faint)]">{model.latestPayload}</span>
						</li>
					{/each}
				</ul>
			{/if}

			{#if ingest.stuckConsumers.length}
				<div class="mb-2 overflow-hidden rounded-[4px] border border-[var(--sb-error)]">
					{#each ingest.stuckConsumers as consumer (consumer.table)}
						<div class="border-b border-[var(--border-subtle)] px-3.5 py-2 last:border-b-0">
							<div class="flex items-baseline gap-3 font-mono text-[12px]">
								<span class="text-primary font-medium">{consumer.table}</span>
								<span style:color="var(--sb-error)">
									{consumer.stuckSeconds === null
										? 'no offset committed'
										: `stuck for ${formatDuration(consumer.stuckSeconds)}`}
								</span>
								<span class="text-muted-foreground">{consumer.recentErrors} recent errors</span>
							</div>
							<div class="text-muted-foreground code mt-1 truncate text-[11px]" title={consumer.latestError}>
								{consumer.latestError}
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Sorted oldest-arrival-first and capped, so this holds up at 20+ sources:
			     the question is "what is not moving", not "list everything". -->
			<div class="overflow-hidden rounded-[4px] border border-border">
				{#each visibleSources as source (source.name)}
					<div
						class="flex items-center gap-4 border-b border-[var(--border-subtle)] px-3.5 py-2 last:border-b-0"
					>
						<div class="w-[168px] shrink-0">
							<a
								href="/sources/{source.name}"
								class="text-primary code block w-full truncate text-[12px] font-medium hover:underline"
								title={source.name}
								>{source.name}</a
							>
							<div class="text-[var(--sb-text-faint)] font-mono text-[10px]">
								{source.kind === 'kafka' ? 'kafka' : 'adopted'} · {source.boundaryMode}
							</div>
						</div>

						<div class="shrink-0"><Sparkline values={source.live.throughput} width={110} height={22} /></div>

						<div class="w-[70px] shrink-0 text-right font-mono text-[12px]" style:color="var(--sb-secondary)">
							{formatRate(source.live.rowsPerSecond)}
						</div>

						<div
							class="w-[76px] shrink-0 text-right font-mono text-[12px]"
							style:color={source.live.freshness === 'stalled'
								? 'var(--sb-error)'
								: source.live.freshness === 'lagging'
									? 'var(--sb-warning)'
									: 'var(--muted-foreground)'}
						>
							{source.live.lastArrivalSeconds === null
								? '—'
								: formatDuration(source.live.lastArrivalSeconds)}
						</div>

						<!-- Per-partition ticks only while they stay legible; beyond that a
						     count plus the worst offender, since some topics have thousands. -->
						<div class="w-[186px] shrink-0">
							{#if source.live.partitions.length === 0}
								<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">no partitions</span>
							{:else if source.live.partitions.length <= PARTITION_TICK_LIMIT}
								<div class="flex items-end gap-[3px]">
									{#each source.live.partitions as partition (partition.partition)}
										<span
											class="w-2.5 rounded-[1px]"
										style:height="{partition.kafkaLagMessages !== null &&
										partition.kafkaLagMessages > 0
											? 7
											: 14}px"
										style:background={partition.kafkaLagMessages === null
											? 'var(--sb-text-faint)'
											: partition.kafkaLagMessages > 0
												? 'var(--sb-warning)'
												: 'var(--sb-secondary)'}
										title="p{partition.partition} · Kafka lag {partition.kafkaLagMessages === null
											? 'unavailable'
											: `${formatCompact(partition.kafkaLagMessages)} messages`}"
										></span>
									{/each}
								</div>
							{:else}
								{@const behind = source.live.partitions.filter(
									(partition) => partition.kafkaLagMessages !== null && partition.kafkaLagMessages > 0
								).length}
								<span class="text-muted-foreground whitespace-nowrap font-mono text-[10.5px]">
									{source.live.partitions.length} partitions
									{#if behind}<span style:color="var(--sb-warning)"> · {behind} behind</span>{/if}
								</span>
							{/if}
							{#if source.live.partitions.length > 0 && source.live.partitions.length <= PARTITION_TICK_LIMIT}
								{@const behind = source.live.partitions.filter(
									(partition) => partition.kafkaLagMessages !== null && partition.kafkaLagMessages > 0
								).length}
								<div class="text-muted-foreground pt-1 font-mono text-[10px]">
									{source.live.partitions.length - behind}/{source.live.partitions.length} caught up
									{#if behind}<span style:color="var(--sb-warning)"> · {behind} behind</span>{/if}
								</div>
							{/if}
						</div>

						<div class="ml-auto shrink-0 text-right">
							<div class="font-mono text-[11px]">{formatCompact(source.live.rows)} rows</div>
							<div
								class="font-mono text-[10px]"
								style:color={source.retentionDays === null
									? 'var(--sb-success)'
									: 'var(--muted-foreground)'}
							>
								{source.retentionDays === null ? 'no TTL' : `${source.retentionDays}d horizon`}
							</div>
						</div>
					</div>
				{/each}

				{#if sortedSources.length > SOURCE_VISIBLE_LIMIT}
					<button
						class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] w-full px-3.5 py-2 text-left font-mono text-[11px]"
						onclick={() => (showAllSources = !showAllSources)}
					>
						{showAllSources
							? 'Show fewer'
							: `Show ${hiddenSourceCount} more sources →`}
					</button>
				{/if}
			</div>
		</div>

		<WarehouseHealthSummary health={project.warehouseHealth} referenceTime={project.capturedAt} />

		<!-- ── freshness + quality ─────────────────────────────────────────── -->
		<div class="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
			<div>
				<div
					class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
				>
					Expected freshness <span class="normal-case tracking-normal">— {freshness.total} models</span>
				</div>
				<div class="rounded-[4px] border border-border p-3.5">
					{#if freshness.monitored === 0}
						<div class="text-[13px] font-medium">Not configured</div>
						<div class="text-muted-foreground mt-1 text-[12px]">
							{monitoredSources} of {project.sources.length} source policies configured · {freshness.unmonitored}
							models not monitored
						</div>
						<div class="text-muted-foreground mt-2 text-[11.5px]">
							Live Kafka transport health remains measured in Ingest above.
						</div>
						<a href="/sources" class="text-primary mt-2 inline-block font-mono text-[11px] hover:underline">Review sources →</a>
					{:else}
					<div class="flex h-3.5 gap-[3px] overflow-hidden rounded-[2px]">
						{#each Array(freshness.fresh) as _, index (`f${index}`)}
							<span class="flex-1 bg-[var(--sb-success)]" title="fresh"></span>
						{/each}
						{#each Array(freshness.lagging) as _, index (`l${index}`)}
							<span class="flex-1 bg-[var(--sb-warning)]" title="lagging"></span>
						{/each}
						{#each Array(freshness.stalled) as _, index (`s${index}`)}
							<span class="flex-1 bg-[var(--sb-error)]" title="stalled"></span>
						{/each}
						{#each Array(freshness.unknown) as _, index (`u${index}`)}
							<span class="flex-1 bg-[var(--sb-border-strong)]" title="not monitored"></span>
						{/each}
					</div>
					<div class="flex flex-wrap gap-4 pt-2.5 font-mono text-[11px]">
						<span class="flex items-center gap-1.5"
							><span class="h-1.5 w-1.5 rounded-[2px] bg-[var(--sb-success)]"></span>{freshness.fresh}
							fresh</span
						>
						<span class="flex items-center gap-1.5"
							><span class="h-1.5 w-1.5 rounded-[2px] bg-[var(--sb-warning)]"></span>{freshness.lagging}
							lagging</span
						>
						<span class="flex items-center gap-1.5"
							><span class="h-1.5 w-1.5 rounded-[2px] bg-[var(--sb-error)]"></span>{freshness.stalled}
							stalled</span
						>
						<span class="flex items-center gap-1.5"
							><span class="h-1.5 w-1.5 rounded-[2px] bg-[var(--sb-border-strong)]"></span>{freshness.unmonitored}
							not monitored</span
						>
					</div>

					{#if freshness.offenders.length}
						<div class="mt-3 border-t border-[var(--border-subtle)] pt-2.5">
							{#each freshness.offenders as model (model.name)}
								<div class="flex items-center gap-3 border-b border-[var(--border-subtle)] py-1.5 last:border-b-0">
									<a
										href="/catalog/{model.name}"
										class="text-primary code w-[190px] shrink-0 truncate text-[11.5px] hover:underline"
										>{model.name}</a
									>
									<span class="w-[84px] shrink-0"><StatusPill status={model.status} /></span>
									<span class="text-muted-foreground font-mono text-[11px]">
										{#if model.status === 'drift'}
											definition changed since last build
										{:else}
											last row {formatAgo(model.live.newestRowAt, project.capturedAt)}
										{/if}
									</span>
									<span class="text-[var(--sb-text-faint)] ml-auto shrink-0 font-mono text-[10.5px]"
										>{model.pipeline}</span
									>
								</div>
							{/each}
						</div>
					{/if}
					{/if}
				</div>
			</div>

			<div>
				<div
					class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
				>
					Quality
					<span class="normal-case tracking-normal"
						>— audits checked {formatAgo(project.audits[0]?.result?.checkedAt ?? null, project.capturedAt)}</span
					>
				</div>
				<div class="rounded-[4px] border border-border p-3.5">
					<div class="grid grid-cols-2 gap-4">
						<div>
							<div class="text-[var(--sb-text-faint)] pb-1 font-mono text-[10px]">Audits</div>
							<div class="flex items-baseline gap-1.5">
								<span class="font-display text-[22px] font-semibold leading-none"
									>{audits.passing}</span
								>
								<span class="text-muted-foreground font-mono text-[12px]">/ {audits.total}</span>
							</div>
							<div class="flex gap-3 pt-1.5 font-mono text-[10.5px]">
								{#if audits.warning}
									<span style:color="var(--sb-warning)">{audits.warning} warn</span>
								{/if}
								{#if audits.failing}
									<span style:color="var(--sb-error)">{audits.failing} fail</span>
								{/if}
							</div>
						</div>
						<div data-testid="quality-tests-summary">
							<div class="text-[var(--sb-text-faint)] pb-1 font-mono text-[10px]">Tests</div>
							{#if tests.executed === 0 && tests.total > 0}
								<div class="text-[13px] font-medium">Not run on {project.target}</div>
								<div class="text-muted-foreground pt-1 font-mono text-[10.5px]">{tests.total} configured</div>
							{:else}
							<div class="flex items-baseline gap-1.5">
								<span class="font-display text-[22px] font-semibold leading-none"
									>{tests.passing}</span
								>
								<span class="text-muted-foreground font-mono text-[12px]">/ {tests.total}</span>
							</div>
							{/if}
							<div class="flex gap-3 pt-1.5 font-mono text-[10.5px]">
								{#if tests.failing}
									<span style:color="var(--sb-error)">{tests.failing} fail</span>
								{/if}
							</div>
						</div>
					</div>

					{#if worstAudit && worstAudit.result}
						<div class="mt-3 border-t border-[var(--border-subtle)] pt-2.5">
							<div class="flex items-center gap-2">
								<span
									class="h-1.5 w-1.5 shrink-0 rounded-[2px]"
									style:background={worstAudit.severity === 'warning'
										? 'var(--sb-warning)'
										: 'var(--sb-error)'}
								></span>
								<code class="truncate font-mono text-[11px]">{worstAudit.name}</code>
								<span class="ml-auto shrink-0 font-mono text-[10.5px]" style:color="var(--sb-error)"
									>{formatInteger(worstAudit.result.failingRowCount)} rows</span
								>
							</div>
						</div>
					{/if}
					<a href="/quality" class="text-primary mt-2.5 inline-block font-mono text-[11px] hover:underline"
						>Open Quality →</a
					>
				</div>
			</div>
		</div>

		<!-- ── snapshot footer ─────────────────────────────────────────────── -->
		<div
			class="text-[var(--sb-text-faint)] flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-border pt-3 font-mono text-[10.5px]"
		>
			<span>snapshot {formatTimestamp(project.capturedAt)}</span>
			<span>target {project.target} · database {project.database}</span>
			<span>adapter {project.adapter}</span>
			<span>direct mode</span>
			<span class="ml-auto"
				>{project.pipelines.length} pipelines · {project.models.length} models · {project.sources
					.length} sources</span
			>
		</div>
	</div>
</div>
