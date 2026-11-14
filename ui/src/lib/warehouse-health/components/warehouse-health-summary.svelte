<script lang="ts">
	import { base } from '$app/paths';
	import ArrowUpRightIcon from '@lucide/svelte/icons/arrow-up-right';
	import type { WarehouseHealth } from '$lib/warehouse-health/types';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatWarehouseBytes } from '$lib/warehouse-health/main/format-warehouse-bytes';
	import { warehouseHealthSummary } from '$lib/warehouse-health/main/warehouse-health-summary';
	import WarehouseHealthStatus from './warehouse-health-status.svelte';

	type Props = {
		health: WarehouseHealth | null;
		referenceTime: string;
	};

	let { health, referenceTime }: Props = $props();
	/** The UI demo's engine runs in the browser, which has no disk, inodes or server memory to report. */
	const DEMO: boolean = import.meta.env.VITE_SQLTIDE_DEMO === '1';
	const primaryDisk = $derived.by(() => {
		if (!health) return null;
		return (
			health.disks.find((disk) => disk.status === 'critical') ??
			health.disks.find((disk) => disk.status === 'warning') ??
			health.disks.find((disk) => disk.status === 'healthy') ??
			health.disks[0] ??
			null
		);
	});
</script>

<section data-testid="warehouse-health-summary">
	<div class="text-[var(--sb-text-faint)] flex items-baseline pb-2 font-mono text-[10px] uppercase tracking-[0.14em]">
		Warehouse
		{#if health}
			<span class="ml-auto normal-case tracking-normal">
				{health.adapter}{health.version ? ` ${health.version}` : ''} · {formatAgo(health.measuredAt, referenceTime)}
			</span>
		{/if}
	</div>
	<div class="rounded-[4px] border border-border">
		<div class="flex items-center gap-3 border-b border-[var(--border-subtle)] px-3.5 py-2.5">
			{#if health}
				<!-- The status grades disk and inode capacity, which the UI demo's in-browser engine does not have. -->
				{#if !DEMO}<WarehouseHealthStatus status={health.status} />{/if}
				<span class="text-muted-foreground text-[11.5px]">
					{health.stale ? 'Last usable evidence. ' : ''}{warehouseHealthSummary(health)}
				</span>
			{:else}
				<span class="text-muted-foreground text-[11.5px]">No warehouse diagnostics in this snapshot.</span>
			{/if}
			<a href="{base}/warehouse-health" class="text-primary ml-auto inline-flex items-center gap-1 font-mono text-[10.5px] hover:underline">
				Inspect <ArrowUpRightIcon size={11} />
			</a>
		</div>

		{#if health}
			<div class={DEMO ? 'grid grid-cols-1' : 'grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4'}>
				{#if !DEMO}
				<div class="border-b border-[var(--border-subtle)] px-3.5 py-2.5 sm:border-r xl:border-b-0">
					<div class="text-[var(--sb-text-faint)] font-mono text-[10.5px] uppercase">Available</div>
					<div class="pt-0.5 font-mono text-[13px]">{formatWarehouseBytes(primaryDisk?.unreservedBytes ?? null)}</div>
					<div class="text-muted-foreground truncate font-mono text-[10.5px]">{primaryDisk?.name ?? 'no disk evidence'}</div>
				</div>
				<div class="border-b border-[var(--border-subtle)] px-3.5 py-2.5 xl:border-r xl:border-b-0">
					<div class="text-[var(--sb-text-faint)] font-mono text-[10.5px] uppercase">Inodes free</div>
					<div class="pt-0.5 font-mono text-[13px]">{health.inodes.free === null ? '—' : formatInteger(health.inodes.free)}</div>
					<div class="text-muted-foreground font-mono text-[10.5px]">main data path</div>
				</div>
				<div class="border-b border-[var(--border-subtle)] px-3.5 py-2.5 sm:border-r sm:border-b-0 xl:border-r">
					<div class="text-[var(--sb-text-faint)] font-mono text-[10.5px] uppercase">Server RSS</div>
					<div class="pt-0.5 font-mono text-[13px]">{formatWarehouseBytes(health.memory?.residentBytes ?? null)}</div>
					<div class="text-muted-foreground font-mono text-[10.5px]">{health.memory === null ? 'context unavailable' : health.memory.basis === 'cgroup' ? 'cgroup context' : 'host context'}</div>
				</div>
				{/if}
				<div class="px-3.5 py-2.5">
					<div class="text-[var(--sb-text-faint)] font-mono text-[10.5px] uppercase">Active now</div>
					<div class="pt-0.5 font-mono text-[13px]">{health.activity?.activeQueries ?? '—'} queries</div>
					<div class="text-muted-foreground font-mono text-[10.5px]">{health.activity?.activeMerges ?? '—'} merges · {health.activity?.incompleteMutations ?? '—'} mutations</div>
				</div>
			</div>
		{/if}
	</div>
</section>
