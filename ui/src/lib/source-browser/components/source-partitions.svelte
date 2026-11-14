<script lang="ts">
	import { base } from '$app/paths';
	import type { PartitionState, Source } from '$lib/domain/types';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';

	let { sourceName, source }: { sourceName: string; source: Source } = $props();

	const PARTITION_PAGE_SIZE: number = 25;
	const LAG_BUCKET_COUNT: number = 48;

	let partitionQuery = $state<string>('');
	let partitionSort = $state<'lag' | 'id'>('lag');
	let partitionPage = $state<number>(0);

	const behindCount = $derived(
		source.live.partitions.filter(
			(partition) => partition.kafkaLagMessages !== null && partition.kafkaLagMessages > 0
		).length
	);

	const lagBuckets = $derived.by(
		(): { label: string; count: number; behind: boolean }[] => {
			const lagValues: number[] = source.live.partitions.flatMap((partition) =>
				partition.kafkaLagMessages === null ? [] : [partition.kafkaLagMessages]
			);
			const maxLag: number = Math.max(...lagValues, 1);
			const width: number = maxLag / LAG_BUCKET_COUNT;
			const buckets: { label: string; count: number; behind: boolean }[] = Array.from(
				{ length: LAG_BUCKET_COUNT },
				(_, index) => ({
					label: formatCompact(Math.round(index * width)),
					count: 0,
					behind: index > 0
				})
			);
			for (const lag of lagValues) {
				const index: number = Math.min(Math.floor(lag / width), LAG_BUCKET_COUNT - 1);
				buckets[index].count += 1;
			}
			return buckets;
		}
	);

	const maxBucketCount = $derived(Math.max(...lagBuckets.map((bucket) => bucket.count), 1));
	const filteredPartitions = $derived.by((): PartitionState[] => {
		const needle: string = partitionQuery.trim();
		const partitions: PartitionState[] = source.live.partitions.filter(
			(partition) => needle === '' || String(partition.partition).includes(needle)
		);
		return [...partitions].sort((a, b) =>
			partitionSort === 'lag'
				? (b.kafkaLagMessages ?? -1) - (a.kafkaLagMessages ?? -1)
				: a.partition - b.partition
		);
	});
	const pageCount = $derived(
		Math.max(Math.ceil(filteredPartitions.length / PARTITION_PAGE_SIZE), 1)
	);
	const pagedPartitions = $derived(
		filteredPartitions.slice(
			Math.min(partitionPage, pageCount - 1) * PARTITION_PAGE_SIZE,
			(Math.min(partitionPage, pageCount - 1) + 1) * PARTITION_PAGE_SIZE
		)
	);

	function partitionMessagesHref(partition: number): string {
		const document: {
			mode: { kind: string; partition: number; fromOffset: null; toOffset: null };
			predicates: never[];
			limit: number;
			timeColumn: string;
			previewPaths: never[];
		} = {
			mode: { kind: 'offsetRange', partition, fromOffset: null, toOffset: null },
			predicates: [],
			limit: 50,
			timeColumn: 'landed',
			previewPaths: []
		};
		return `${base}/sources/${sourceName}/messages?q=${encodeURIComponent(JSON.stringify(document))}`;
	}
</script>

<div>
	<div class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]">
		Partitions
		<span class="normal-case tracking-normal">
			{formatInteger(source.live.partitions.length)} total{#if behindCount}
				· <span style:color="var(--sb-warning)">{behindCount} behind</span>{/if}
		</span>
	</div>

	<div class="rounded-[4px] border border-border p-3">
		<div class="flex h-9 items-end gap-[2px]">
			{#each lagBuckets as bucket, bucketIndex (bucketIndex)}
				<div
					class="flex-1 rounded-[1px]"
					style:height="{bucket.count ? Math.max(Math.sqrt(bucket.count / maxBucketCount) * 100, 12) : 3}%"
					style:background={bucket.behind ? 'var(--sb-warning)' : 'var(--sb-secondary)'}
					style:opacity={bucket.count ? 0.75 : 0.15}
					title="{bucket.label} · {bucket.count} partitions"
				></div>
			{/each}
		</div>
		<div class="text-[var(--sb-text-faint)] flex justify-between pt-1.5 font-mono text-[10px]">
			<span>Kafka lag distribution</span>
			<span>{lagBuckets[0].label} → {lagBuckets[lagBuckets.length - 1].label} messages</span>
		</div>
	</div>

	<div class="flex items-center gap-2 py-2">
		<input
			bind:value={partitionQuery}
			placeholder="partition id…"
			class="bg-[var(--sb-inset)] w-[140px] rounded-[4px] border border-border px-2.5 py-1 font-mono text-[11px] outline-none focus:border-[var(--primary)]"
		/>
		<div class="flex overflow-hidden rounded-[4px] border border-border">
			<button
				class="px-2.5 py-1 font-mono text-[10.5px] {partitionSort === 'lag' ? 'bg-[var(--sb-hover)] text-foreground' : 'text-muted-foreground hover:text-foreground'}"
				onclick={() => (partitionSort = 'lag')}>worst Kafka lag</button
			>
			<button
				class="border-l border-border px-2.5 py-1 font-mono text-[10.5px] {partitionSort === 'id' ? 'bg-[var(--sb-hover)] text-foreground' : 'text-muted-foreground hover:text-foreground'}"
				onclick={() => (partitionSort = 'id')}>id</button
			>
		</div>
		<span class="text-muted-foreground ml-auto font-mono text-[10.5px]">
			{filteredPartitions.length === source.live.partitions.length
				? `${formatInteger(filteredPartitions.length)} partitions`
				: `${formatInteger(filteredPartitions.length)} of ${formatInteger(source.live.partitions.length)}`}
		</span>
	</div>

	<table class="sb-list min-w-[760px] w-full text-left">
		<thead>
			<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
				<th class="px-3 py-2 font-normal">Partition</th>
				<th class="px-3 py-2 font-normal">Landed offset</th>
				<th class="px-3 py-2 font-normal">Committed</th>
				<th class="px-3 py-2 font-normal">Broker end</th>
				<th class="px-3 py-2 font-normal">Kafka lag</th>
				<th class="px-3 py-2 font-normal">Last arrival</th>
			</tr>
		</thead>
		<tbody>
			{#each pagedPartitions as partition (partition.partition)}
				<tr>
					<td class="code px-3 text-[12px]">
						{#if source.kind === 'kafka'}
							<a href={partitionMessagesHref(partition.partition)} class="text-primary hover:underline" title="browse this partition's messages">{partition.partition}</a>
						{:else}
							{partition.partition}
						{/if}
					</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]">{partition.offset === null ? '—' : formatInteger(partition.offset)}</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]">{partition.committedOffset === null ? '—' : formatInteger(partition.committedOffset)}</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]">{partition.endOffset === null ? '—' : formatInteger(partition.endOffset)}</td>
					<td class="code px-3 text-[11.5px]">
						{#if partition.kafkaLagMessages === null}
							<span class="text-[var(--sb-text-faint)]">—</span>
						{:else}
							<span style:color={partition.kafkaLagMessages > 0 ? 'var(--sb-warning)' : 'var(--foreground)'}>{formatCompact(partition.kafkaLagMessages)} msg</span>
						{/if}
					</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]">{formatTimestamp(partition.newestEventAt)}</td>
				</tr>
			{/each}
		</tbody>
	</table>

	{#if pageCount > 1}
		<div class="flex items-center gap-2 pt-2">
			<button class="text-muted-foreground hover:text-foreground rounded-[4px] border border-border px-2.5 py-1 font-mono text-[10.5px] disabled:opacity-40" disabled={partitionPage === 0} onclick={() => (partitionPage -= 1)}>← prev</button>
			<span class="text-muted-foreground font-mono text-[10.5px]">page {partitionPage + 1} of {pageCount}</span>
			<button class="text-muted-foreground hover:text-foreground rounded-[4px] border border-border px-2.5 py-1 font-mono text-[10.5px] disabled:opacity-40" disabled={partitionPage >= pageCount - 1} onclick={() => (partitionPage += 1)}>next →</button>
		</div>
	{/if}
</div>
