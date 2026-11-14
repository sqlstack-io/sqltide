<script lang="ts">
	import HardDriveIcon from '@lucide/svelte/icons/hard-drive';
	import InfoIcon from '@lucide/svelte/icons/info';
	import MemoryStickIcon from '@lucide/svelte/icons/memory-stick';
	import MergeIcon from '@lucide/svelte/icons/merge';
	import type { Project } from '$lib/domain/types';
	import type { WarehouseDiskHealth, WarehouseHealth } from '$lib/warehouse-health/types';
	import { getProject } from '$lib/api/main/project/get-project';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatPercent } from '$lib/formatting/main/format-percent';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import WarehouseHealthStatus from '$lib/warehouse-health/components/warehouse-health-status.svelte';
	import { formatWarehouseBytes } from '$lib/warehouse-health/main/format-warehouse-bytes';
	import { warehouseHealthTone } from '$lib/warehouse-health/main/warehouse-health-tone';
	import { warehouseHealthSummary } from '$lib/warehouse-health/main/warehouse-health-summary';

	const project: Project = getProject();
	const health = $derived<WarehouseHealth | null>(project.warehouseHealth);

	function availableFraction(disk: WarehouseDiskHealth): number | null {
		return disk.totalBytes !== null && disk.totalBytes > 0 && disk.unreservedBytes !== null
			? disk.unreservedBytes / disk.totalBytes
			: null;
	}
</script>

<AppTopbar title="Warehouse Health" />

<div class="min-h-0 flex-1 overflow-y-auto" data-testid="warehouse-health-page">
	{#if health === null}
		<div class="m-[18px] rounded-[4px] border border-border p-5">
			<div class="flex items-center gap-2 font-mono text-[12px]"><InfoIcon size={14} /> Diagnostics unavailable</div>
			<p class="text-muted-foreground mt-2 max-w-2xl text-[12px]">
				The current state snapshot does not contain warehouse diagnostics. Refresh the snapshot or
				check whether this adapter supports operational health reads.
			</p>
		</div>
	{:else}
		<div class="flex flex-col gap-5 p-[18px]">
			<section class="rounded-[4px] border border-border">
				<div class="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[var(--border-subtle)] px-3.5 py-2.5">
					<WarehouseHealthStatus status={health.status} />
					<span class="font-mono text-[11px]">{health.adapter}{health.version ? ` ${health.version}` : ''}</span>
					<span class="text-muted-foreground font-mono text-[10.5px]">{health.database}</span>
					<span class="text-muted-foreground ml-auto font-mono text-[10.5px]">
						measured {formatAgo(health.measuredAt, project.capturedAt)} · {health.collectionDurationMs}ms
					</span>
				</div>
				<div class="px-3.5 py-3">
					<div class="text-[13px] font-medium">{warehouseHealthSummary(health)}</div>
					<div class="text-muted-foreground mt-1 text-[12px]">
						Point-in-time read-only diagnostics measured {formatAgo(health.measuredAt, project.capturedAt)} in
						{health.collectionDurationMs}ms.
					</div>
				</div>
				{#if health.stale || health.warnings.length}
					<div class="px-3.5 py-2.5 text-[11.5px]" style:color={health.stale ? 'var(--sb-warning)' : 'var(--muted-foreground)'}>
						{health.stale ? 'Showing the last usable evidence. ' : ''}{health.warnings.join(' ')}
					</div>
				{/if}
			</section>

			<section>
				<div class="text-[var(--sb-text-faint)] flex items-center gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]">
					<HardDriveIcon size={12} /> Capacity
				</div>
				{#if health.disks.length === 0}
					<div class="rounded-[4px] border border-border p-3.5 text-[12px] text-muted-foreground">No usable disk evidence was returned.</div>
				{:else}
					<div class="grid gap-3" style:grid-template-columns="repeat(auto-fit, minmax(260px, 1fr))">
						{#each health.disks as disk (disk.name)}
							{@const available = availableFraction(disk)}
							<div class="rounded-[4px] border border-border p-3.5">
								<div class="flex items-center gap-2">
									<span class="font-mono text-[12px] font-medium">{disk.name}</span>
									<span class="text-muted-foreground font-mono text-[12px]">{disk.type ?? 'Unknown type'}</span>
									<span class="ml-auto"><WarehouseHealthStatus status={disk.status} /></span>
								</div>
								<div class="mt-3 h-2 overflow-hidden rounded-[2px] bg-[var(--sb-hover)]">
									<div
										class="h-full"
										style:width={`${Math.max(0, Math.min(1, available ?? 0)) * 100}%`}
										style:background={warehouseHealthTone(disk.status)}
									></div>
								</div>
								<div class="mt-2 grid grid-cols-3 gap-3 font-mono text-[12px]">
									<div><span class="text-[var(--sb-text-faint)] block text-[11px]">Available</span>{formatWarehouseBytes(disk.unreservedBytes)}</div>
									<div><span class="text-[var(--sb-text-faint)] block text-[11px]">Free</span>{formatWarehouseBytes(disk.freeBytes)}</div>
									<div><span class="text-[var(--sb-text-faint)] block text-[11px]">Total</span>{formatWarehouseBytes(disk.totalBytes)}</div>
								</div>
								<div class="text-muted-foreground mt-2 flex gap-3 font-mono text-[12px]">
									<span>{available === null ? 'unknown available' : `${formatPercent(available)} available`}</span>
									<span class="truncate" title={disk.path ?? undefined}>{disk.path ?? 'path unavailable'}</span>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</section>

			<section class="grid gap-3" style:grid-template-columns="repeat(auto-fit, minmax(220px, 1fr))">
				<div class="rounded-[4px] border border-border p-3.5">
					<div class="text-[var(--sb-text-faint)] flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.12em]"><HardDriveIcon size={12} /> Main-path inodes</div>
					<div class="mt-3 flex items-end gap-2"><span class="font-mono text-[20px]">{health.inodes.free === null ? '—' : formatInteger(health.inodes.free)}</span><span class="text-muted-foreground pb-1 text-[10px]">free</span></div>
					<div class="text-muted-foreground font-mono text-[10px]">{health.inodes.total === null ? 'total unavailable' : `${formatInteger(health.inodes.total)} total`}</div>
					<div class="mt-2"><WarehouseHealthStatus status={health.inodes.status} /></div>
				</div>

				<div class="rounded-[4px] border border-border p-3.5">
					<div class="text-[var(--sb-text-faint)] flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.12em]"><MemoryStickIcon size={12} /> Memory context</div>
					<div class="mt-3 font-mono text-[20px]">{formatWarehouseBytes(health.memory?.residentBytes ?? null)}</div>
					<div class="text-muted-foreground font-mono text-[10px]">ClickHouse server RSS</div>
					{#if health.memory?.basis === 'cgroup'}
						<div class="mt-2 font-mono text-[10.5px]">{formatWarehouseBytes(health.memory.cgroupUsedBytes)} / {formatWarehouseBytes(health.memory.cgroupLimitBytes)} cgroup</div>
					{:else if health.memory !== null}
						<div class="mt-2 font-mono text-[10.5px]">{formatWarehouseBytes(health.memory?.hostTotalBytes ?? null)} host memory</div>
					{:else}
						<div class="mt-2 font-mono text-[10.5px]">memory context unavailable</div>
					{/if}
				</div>

				<div class="rounded-[4px] border border-border p-3.5">
					<div class="text-[var(--sb-text-faint)] flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.12em]"><MergeIcon size={12} /> Current activity</div>
					<div class="mt-3 grid grid-cols-3 gap-2 text-center font-mono">
						<div><div class="text-[20px]">{health.activity?.activeQueries ?? '—'}</div><div class="text-muted-foreground text-[12px]">active queries</div></div>
						<div><div class="text-[20px]">{health.activity?.activeMerges ?? '—'}</div><div class="text-muted-foreground text-[12px]">active merges</div></div>
						<div><div class="text-[20px]">{health.activity?.incompleteMutations ?? '—'}</div><div class="text-muted-foreground text-[12px]">pending mutations</div></div>
					</div>
				</div>

				<div class="rounded-[4px] border border-border p-3.5">
					<div class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.12em]">Server</div>
					<div class="mt-3 font-mono text-[20px]">{health.uptimeSeconds === null ? '—' : formatDuration(health.uptimeSeconds)}</div>
					<div class="text-muted-foreground font-mono text-[10px]">uptime</div>
					<div class="mt-2 font-mono text-[10.5px]">{health.availability} evidence</div>
				</div>
			</section>

			<section>
				<div class="text-[var(--sb-text-faint)] flex items-baseline pb-2 font-mono text-[10px] uppercase tracking-[0.14em]">
					Largest project tables <span class="ml-auto normal-case tracking-normal">active parts in {health.database}</span>
				</div>
				<div class="overflow-x-auto rounded-[4px] border border-border">
					<table class="sb-list w-full text-left">
						<thead>
							<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.12em]">
								<th class="px-3 py-2 font-normal">Table</th>
								<th class="px-3 py-2 text-right font-normal">Rows</th>
								<th class="px-3 py-2 text-right font-normal">Disk</th>
								<th class="px-3 py-2 text-right font-normal">Active parts</th>
							</tr>
						</thead>
						<tbody>
							{#if health.tables === null}
								<tr><td colspan="4" class="code text-muted-foreground px-3 text-[11.5px]">Project table footprint is unavailable.</td></tr>
							{:else if health.tables.length === 0}
								<tr><td colspan="4" class="code text-muted-foreground px-3 text-[11.5px]">No active MergeTree parts were reported for this project database.</td></tr>
							{:else}
								{#each health.tables as table (table.name)}
									<tr>
										<td class="code px-3 text-[11.5px]">{table.name}</td>
										<td class="code px-3 text-right text-[11.5px]">{table.rows === null ? '—' : formatInteger(table.rows)}</td>
										<td class="code px-3 text-right text-[11.5px]">{formatWarehouseBytes(table.bytesOnDisk)}</td>
										<td class="code px-3 text-right text-[11.5px]">{table.activeParts === null ? '—' : formatInteger(table.activeParts)}</td>
									</tr>
								{/each}
							{/if}
						</tbody>
					</table>
				</div>
			</section>

			<section class="flex gap-2.5 rounded-[4px] border border-border px-3.5 py-3 text-[11px] text-muted-foreground">
				<InfoIcon size={13} class="mt-0.5 shrink-0" />
				<p>Read-only point-in-time diagnostics from ClickHouse system tables. Memory values remain separately labelled when only server RSS and host capacity are available. SQLTide does not retain metric history or perform remediation.</p>
			</section>
		</div>
	{/if}
</div>
