<script lang="ts">
	import FactRow from '$lib/presentation/components/fact-row.svelte';
	import Sparkline from '$lib/presentation/components/sparkline.svelte';
	import { REPLAY_COLUMN_BY_ROLE } from '$lib/domain/constants';
	import type { ReplayRole, Source } from '$lib/domain/types';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatRate } from '$lib/formatting/main/format-rate';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';

	let { source }: { source: Source } = $props();

	const MANAGED_RELATION_LABEL: Record<string, string> = {
		kafka_engine: 'Kafka engine',
		landing_mv: 'landing MV',
		landing_table: 'landing table'
	};
</script>

<div class="flex flex-col gap-5">
	<div>
		<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Live</div>
		<div class="pb-2"><Sparkline values={source.live.throughput} width={280} height={34} /></div>
		{#if source.live.throughputWindowSeconds}
			<div class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px]">last {formatDuration(source.live.throughputWindowSeconds)}</div>
		{/if}
		{#if source.live.consumerGroupDrift}
			<div
				class="mb-2 border px-2.5 py-2 font-mono text-[11px]"
				style:border-color="color-mix(in srgb, var(--sb-warning) 45%, var(--border))"
				style:background="color-mix(in srgb, var(--sb-warning) 10%, var(--background))"
				data-testid="consumer-group-drift"
			>
				<div class="font-semibold" style:color="var(--sb-warning)">Consumer group drift</div>
				<div class="mt-0.5 text-foreground">{source.live.consumerGroupDrift.message}</div>
			</div>
		{/if}
		<FactRow label="Rate" value={formatRate(source.live.rowsPerSecond)} />
		<FactRow label="Kafka lag" value={source.live.kafkaLagMessages === null ? 'unavailable' : `${formatCompact(source.live.kafkaLagMessages)} messages`} />
		<FactRow label="Last arrival" value={source.live.lastArrivalSeconds === null ? 'unavailable' : `${formatDuration(source.live.lastArrivalSeconds)} ago`} />
		<FactRow label="Retained rows" value={formatCompact(source.live.rows)} />
		<FactRow label="Newest event" value={formatTimestamp(source.live.newestEventAt)} />
		<FactRow label="Retained from" value={formatTimestamp(source.live.oldestEventAt)} />
	</div>

	<div>
		<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Configuration</div>
		<FactRow label="Kind" value={source.kind} mono />
		<FactRow label="Boundary" value={source.boundaryMode} mono />
		{#if source.brokerList}<FactRow label="Broker" value={source.brokerList} mono />{/if}
		{#if source.topic}<FactRow label="Topic" value={source.topic} mono />{/if}
		{#if source.consumerGroup}<FactRow label="Consumer group" value={source.consumerGroup} mono />{/if}
		{#if source.format}<FactRow label="Format" value={source.format} mono />{/if}
		<FactRow label="Read relation" value={source.relationName} mono />
		{#if source.settings}
			{#each Object.entries(source.settings) as [key, value] (key)}
				<FactRow label={key} value={value} mono />
			{/each}
		{/if}
	</div>

	{#if source.managedRelations.length}
		<div>
			<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Managed relations</div>
			{#each source.managedRelations as relation (relation.name)}
				<div class="border-b border-[var(--border-subtle)] py-2">
					<div class="code text-[11.5px]">{relation.name}</div>
					<div class="text-[var(--sb-text-faint)] pt-0.5 font-mono text-[10px]">{MANAGED_RELATION_LABEL[relation.kind]}</div>
				</div>
			{/each}
		</div>
	{:else}
		<div>
			<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Adopted relation</div>
			<div class="code pb-1 text-[11.5px]">{source.relationName}</div>
		</div>
	{/if}

	{#if source.columnMapping}
		<div>
			<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Replay column mapping</div>
			{#each Object.entries(source.columnMapping) as [role, column] (role)}
				<FactRow label={REPLAY_COLUMN_BY_ROLE[role as ReplayRole]} value={column ?? '—'} mono />
			{/each}
		</div>
	{/if}
</div>
