<script lang="ts">
	import { base } from '$app/paths';
	import { SOURCE_KIND_LABEL } from '$lib/domain/constants';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import Sparkline from '$lib/presentation/components/sparkline.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatRate } from '$lib/formatting/main/format-rate';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import type { Project } from '$lib/domain/types';

	const project: Project = getProject();

	const throughputWindow = $derived(
		project.sources.find((source) => source.live.throughputWindowSeconds !== null)?.live
			.throughputWindowSeconds ?? null
	);
</script>

<AppTopbar title="Sources" />

<div class="min-h-0 flex-1 overflow-auto">
	<table class="sb-list min-w-[980px] w-full text-left">
		<thead>
			<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
				<th class="px-[18px] py-2 font-normal">Source</th>
				<th class="px-3 py-2 font-normal">Kind</th>
				<th class="px-3 py-2 font-normal">Origin</th>
				<th class="px-3 py-2 font-normal">Boundary</th>
				<th class="px-3 py-2 font-normal"
					>Throughput{#if throughputWindow}
						<span class="text-[var(--sb-text-faint)] normal-case"
							>(last {formatDuration(throughputWindow)})</span
						>{/if}</th
				>
				<th class="px-3 py-2 font-normal">Kafka lag</th>
				<th class="px-3 py-2 font-normal">Last arrival</th>
				<th class="px-3 py-2 font-normal">Retained</th>
				<th class="px-3 py-2 pr-[18px] font-normal">Reconstruction horizon</th>
			</tr>
		</thead>
		<tbody>
			{#each project.sources as source (source.name)}
				<tr>
					<td class="px-[18px]">
						<a
							href="{base}/sources/{source.name}"
							class="text-primary code text-[12.5px] font-medium hover:underline">{source.name}</a
						>
						<div class="text-[var(--sb-text-faint)] code pt-0.5 text-[10.5px]">
							{source.relationName}
						</div>
					</td>
					<td class="px-3">
						<span class="sb-tag">{SOURCE_KIND_LABEL[source.kind]}</span>
						{#if source.refresh !== null}
							<div class="text-[var(--sb-text-faint)] code pt-0.5 text-[10.5px]">
								refresh every {source.refresh}
							</div>
						{/if}
					</td>
					<td class="text-muted-foreground code px-3 text-[11px]"
						>{source.topic ?? source.relationName}</td
					>
					<td class="px-3"><span class="sb-tag code">{source.boundaryMode}</span></td>
					<td class="px-3">
						<div class="flex items-center gap-2.5">
							<Sparkline values={source.live.throughput} width={92} height={20} />
							<span class="code text-[11.5px]" style:color="var(--sb-secondary)"
								>{formatRate(source.live.rowsPerSecond)}</span
							>
						</div>
					</td>
					<td class="code px-3 text-[11.5px]">
						{#if source.live.kafkaLagMessages === null}
							<span class="text-[var(--sb-text-faint)]">—</span>
						{:else}
							<span
								style:color={source.live.kafkaLagMessages > 0
									? 'var(--sb-warning)'
									: 'var(--foreground)'}>{formatCompact(source.live.kafkaLagMessages)} msg</span
							>
						{/if}
					</td>
					<td class="code px-3 text-[11.5px]">
						{#if source.live.lastArrivalSeconds === null}
							<span class="text-[var(--sb-text-faint)]">—</span>
						{:else}
							<span
								style:color={source.live.freshness === 'stalled' ||
								source.live.freshness === 'lagging'
									? 'var(--sb-warning)'
									: 'var(--foreground)'}>{formatDuration(source.live.lastArrivalSeconds)}</span
							>
						{/if}
					</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]"
						>{formatCompact(source.live.rows)}</td
					>
					<td class="px-3 pr-[18px]">
						{#if source.retentionDays === null}
							<span class="code text-[11.5px]" style:color="var(--sb-success)"
								>unbounded — lossless rebuilds</span
							>
						{:else}
							<span class="code text-[11.5px]">{source.retentionDays}d</span>
							<div class="text-[var(--sb-text-faint)] pt-0.5 text-[10.5px]">
								from {formatTimestamp(source.live.oldestEventAt).slice(0, 10)}
							</div>
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
