<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import MessageSquareTextIcon from '@lucide/svelte/icons/message-square-text';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import ResizableSplitPane from '$lib/presentation/components/resizable-split-pane.svelte';
	import SpanTrack from '$lib/presentation/components/span-track.svelte';
	import SqlBlock from '$lib/presentation/components/sql-block.svelte';
	import type { SqlArtifact } from '$lib/presentation/components/sql-block.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { sourceByName } from '$lib/domain/main/lookups/source-by-name';
	import { reconstructionCoverage } from '$lib/domain/main/reconstruction/reconstruction-coverage';
	import { formatDaySpan } from '$lib/formatting/main/format-day-span';
	import SourcePartitions from '$lib/source-browser/components/source-partitions.svelte';
	import SourceSidebar from '$lib/source-browser/components/source-sidebar.svelte';
	import { sourceDomainFrom } from '$lib/source-browser/main/source-domain-from';
	import type { ManagedRelationKind, Project, Source } from '$lib/domain/types';

	const MANAGED_RELATION_LABEL: Record<ManagedRelationKind, string> = {
		kafka_engine: 'Kafka engine',
		landing_mv: 'landing MV',
		landing_table: 'landing table'
	};

	const MANAGED_RELATION_ORDER: ManagedRelationKind[] = [
		'kafka_engine',
		'landing_table',
		'landing_mv'
	];

	const project: Project = getProject();
	const sourceName = $derived(page.params.name ?? '');
	const source = $derived(sourceByName(project, sourceName));

	const managedArtifacts = $derived.by((): SqlArtifact[] => {
		const relations: Source['managedRelations'] = source?.managedRelations ?? [];
		return MANAGED_RELATION_ORDER.flatMap((kind) =>
			relations
				.filter((relation) => relation.kind === kind && relation.ddl !== null)
				.map((relation) => ({ label: MANAGED_RELATION_LABEL[kind], code: relation.ddl }))
		);
	});

	const coverage = $derived(
		reconstructionCoverage(project).filter((row) => row.sourceName === sourceName)
	);
	const truncating = $derived(coverage.filter((row) => row.state === 'truncating'));

	const domainFrom = $derived(sourceDomainFrom(source, coverage, project.capturedAt));

</script>

<AppTopbar title={sourceName} />

<div class="min-h-0 flex-1 overflow-auto">
	{#if !source}
		<div class="px-[18px] py-10 text-center">
			<p class="text-muted-foreground text-[13px]">No source named <code>{sourceName}</code>.</p>
			<a href="{base}/sources" class="text-primary mt-2 inline-block font-mono text-[11.5px]"
				>← Back to sources</a
			>
		</div>
	{:else}
		<div class="flex items-center gap-2.5 border-b border-border px-[18px] py-2.5">
			<a
				href="{base}/sources"
				class="text-muted-foreground hover:text-foreground flex items-center gap-1 font-mono text-[11px]"
				><ArrowLeftIcon size={12} /> Sources</a
			>
			<span class="text-[var(--sb-text-faint)]">/</span>
			<span class="sb-tag">{source.kind === 'kafka' ? 'managed by SQLTide' : 'adopted'}</span>
			<span class="sb-tag code">{source.boundaryMode}</span>
			{#if source.kind === 'kafka'}
				<a
					href="{base}/sources/{sourceName}/messages"
					class="text-primary ml-auto flex items-center gap-1 font-mono text-[11px] hover:underline"
					><MessageSquareTextIcon size={12} /> Browse messages</a
				>
			{/if}
		</div>

		<ResizableSplitPane storageKey="sb-source-detail-sidebar-width">
			{#snippet main()}
			<div class="flex min-w-0 flex-col gap-5">
				<!-- ── RECONSTRUCTION HORIZON ──────────────────────────────────────
				     The single config value that defines the durability of everything
				     downstream. Stated with its consequence, in plain words. -->
				<div class="rounded-[4px] border border-border p-3.5">
					<div
						class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						Reconstruction horizon
					</div>
					{#if source.retentionDays === null}
						<div class="font-mono text-[13px]" style:color="var(--sb-success)">
							no TTL — retained indefinitely
						</div>
					{:else}
						<div class="flex items-baseline gap-3">
							<span class="font-display text-[26px] font-semibold leading-none"
								>{source.retentionDays}d</span
							>
							<code class="text-muted-foreground font-mono text-[11.5px]">{source.ttl}</code>
						</div>
					{/if}
				</div>

				<!-- ── RECONSTRUCTION COVERAGE ─────────────────────────────────────
				     A standing latent condition: a model can hold more history than the
				     source can rebuild. The CLI never reports this. -->
				{#if coverage.length}
					<div>
						<div
							class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
						>
							Reconstruction coverage
							<span class="normal-case tracking-normal">— held history vs rebuildable history</span>
						</div>

						<div class="overflow-hidden rounded-[4px] border border-border">
							<div class="border-b border-border px-3 py-2.5">
								<div
									class="text-muted-foreground flex items-baseline gap-2 pb-1 font-mono text-[10px]"
								>
									<span>{source.relationName} retains</span>
									<span class="ml-auto"
										>{source.retentionDays === null
											? 'unbounded'
											: formatDaySpan(source.retentionDays)}</span
									>
								</div>
								<SpanTrack
									{domainFrom}
									domainTo={project.capturedAt}
									height={16}
									bands={[
										{
											from: domainFrom,
											to: source.live.oldestEventAt,
											colour: 'transparent',
											hatch: true,
											label: 'aged out of retention'
										},
										{
											from: source.live.oldestEventAt,
											to: project.capturedAt,
											colour: 'var(--sb-secondary)',
											opacity: 0.55,
											label: 'retained'
										}
									]}
								/>
							</div>

							{#each coverage as row (row.modelName)}
								<div class="border-b border-[var(--border-subtle)] px-3 py-2 last:border-b-0">
									<div class="flex items-baseline gap-2 pb-1">
										<a
											href="{base}/catalog/{row.modelName}"
											class="text-primary code text-[11.5px] hover:underline">{row.modelName}</a
										>
										<span class="text-muted-foreground ml-auto font-mono text-[10px]">
											holds {formatDaySpan(row.heldDays ?? 0)}
										</span>
										{#if row.state === 'truncating'}
											<span class="font-mono text-[10px]" style:color="var(--sb-warning)">
												· {formatDaySpan(row.unreconstructableDays ?? 0)} unreconstructable
											</span>
										{:else if row.state === 'matched'}
											<span class="font-mono text-[10px]" style:color="var(--sb-success)">· matched</span
											>
										{:else if row.state === 'lossless'}
											<span class="font-mono text-[10px]" style:color="var(--sb-success)"
												>· lossless</span
											>
										{/if}
									</div>
									<SpanTrack
										{domainFrom}
										domainTo={project.capturedAt}
										height={12}
										bands={[
											...(row.state === 'truncating' && row.heldFrom && row.retainedFrom
												? [
														{
															from: row.heldFrom,
															to: row.retainedFrom,
															colour: 'var(--sb-warning)',
															opacity: 0.6,
															label: 'held but not rebuildable'
														}
													]
												: []),
											{
												from:
													row.state === 'truncating' && row.retainedFrom
														? row.retainedFrom
														: (row.heldFrom ?? domainFrom),
												to: project.capturedAt,
												colour: 'var(--primary)',
												opacity: 0.5,
												label: 'rebuildable'
											}
										]}
									/>
								</div>
							{/each}
						</div>

						{#if truncating.length}
							<p class="pt-2 text-[12px]" style:color="var(--sb-warning)">
								{truncating.length}
								{truncating.length === 1 ? 'model holds' : 'models hold'} history
								{source.name} can no longer rebuild.
							</p>
						{/if}
					</div>
				{/if}

				<!-- ── PARTITIONS ──────────────────────────────────────────────────
				     Topics with hundreds or thousands of partitions are normal, so this
				     leads with the distribution and the stragglers, and pages the rest. -->
				{#if source.live.partitions.length}
					<SourcePartitions {sourceName} {source} />
				{/if}

				<!-- managed object DDL, rendered by the compiler and served per relation -->
				{#if managedArtifacts.length}
					<div>
						<div
							class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
						>
							Managed objects
						</div>
						<SqlBlock artifacts={managedArtifacts} maxHeight="300px" />
					</div>
				{/if}
			</div>
			{/snippet}

			{#snippet sidebar()}
			<SourceSidebar {source} />
			{/snippet}
		</ResizableSplitPane>
	{/if}
</div>
