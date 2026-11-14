<script lang="ts">
	import { base } from '$app/paths';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { getProject } from '$lib/api/main/project/get-project';
	import { createTopicBrowserState } from '$lib/topic-browser/main/create-topic-browser-state.svelte';
	import type { TopicItem } from '$lib/topic-browser/types';

	const topicsStore = createTopicBrowserState();
	const project = getProject();
	let query = $state<string>('');
	// Managed topics are the point of this page; the full cluster inventory is
	// opt-in noise. Internal topics are a further cut within unmanaged ones.
	let showUnmanaged = $state<boolean>(false);
	let showInternal = $state<boolean>(false);

	const payload = $derived(topicsStore.payload);
	const error = $derived(topicsStore.error);
	const loading = $derived(topicsStore.loading);

	// The store keeps the last inventory across navigations, so this mount
	// renders instantly from cache and revalidates in place. While brokers are
	// still pending server-side, a short follow-up poll fills the gaps.
	$effect(() => topicsStore.start());

	const visibleTopics = $derived.by((): TopicItem[] => {
		const topics: TopicItem[] = payload?.topics ?? [];
		const needle: string = query.trim().toLowerCase();
		return topics
			.filter((topic) => showUnmanaged || topic.sources.length > 0)
			.filter((topic) => showInternal || !topic.internal)
			.filter((topic) => needle === '' || topic.name.toLowerCase().includes(needle));
	});
	const unmanagedCount = $derived(
		(payload?.topics ?? []).filter((topic) => topic.sources.length === 0 && !topic.internal)
			.length
	);
	const totalPartitions = $derived(
		visibleTopics.reduce((sum, topic) => sum + (topic.partitions ?? 0), 0)
	);
	const managedCount = $derived(
		visibleTopics.filter((topic) => topic.sources.length > 0).length
	);
</script>

<AppTopbar title="Topics">
	<div class="text-[var(--sb-text-faint)] flex items-center gap-1.5 font-mono text-[10px]">
		{#if loading}<RefreshCwIcon size={10} class="animate-spin" /> refreshing{/if}
		{#if topicsStore.updatedAt !== null}updated {formatAgo(new Date(topicsStore.updatedAt).toISOString(), project.capturedAt)}{/if}
	</div>
</AppTopbar>

<div class="min-h-0 flex-1 overflow-auto">
	<div class="flex flex-col gap-3 px-[18px] py-3">
		{#if payload && !payload.available}
			<div class="rounded-[4px] border border-border px-4 py-6 text-center">
				<p class="text-muted-foreground text-[13px]">{payload.reason}</p>
			</div>
		{:else}
			<!-- totals strip -->
			<div class="flex flex-wrap items-center gap-5 rounded-[4px] border border-border px-4 py-3">
				<div>
					<div class="font-display text-[20px] font-semibold leading-none">
						{formatInteger(visibleTopics.length)}
					</div>
					<div class="text-[var(--sb-text-faint)] pt-1 font-mono text-[10px] uppercase tracking-[0.14em]">
						topics
					</div>
				</div>
				<div>
					<div class="font-display text-[20px] font-semibold leading-none">
						{formatInteger(totalPartitions)}
					</div>
					<div class="text-[var(--sb-text-faint)] pt-1 font-mono text-[10px] uppercase tracking-[0.14em]">
						partitions
					</div>
				</div>
				<div>
					<div class="font-display text-[20px] font-semibold leading-none">
						{formatInteger(managedCount)}
					</div>
					<div class="text-[var(--sb-text-faint)] pt-1 font-mono text-[10px] uppercase tracking-[0.14em]">
						managed by SQLTide
					</div>
				</div>
				{#if payload && payload.pendingBrokers.length > 0}
					<span class="text-muted-foreground font-mono text-[10.5px]">
						reading broker metadata from {payload.pendingBrokers.join(', ')}…
					</span>
				{/if}
				<button
					class="text-muted-foreground hover:text-foreground ml-auto flex items-center gap-1 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px]"
					onclick={() => void topicsStore.refresh()}
					><RefreshCwIcon size={11} class={loading ? 'animate-spin' : ''} /> refresh</button
				>
			</div>

			{#if error}
				<div
					class="rounded-[4px] border px-3 py-2 font-mono text-[11.5px]"
					style:border-color="var(--sb-danger)"
					style:color="var(--sb-danger)"
				>
					{error}
				</div>
			{/if}

			<div class="flex items-center gap-2">
				<input
					bind:value={query}
					placeholder="search topics…"
					class="bg-[var(--sb-inset)] w-[240px] rounded-[4px] border border-border px-2.5 py-1 font-mono text-[11px] outline-none focus:border-[var(--primary)]"
				/>
				<label class="text-muted-foreground flex items-center gap-1.5 font-mono text-[10.5px]">
					<input type="checkbox" bind:checked={showUnmanaged} />
					unmanaged topics ({formatInteger(unmanagedCount)})
				</label>
				{#if showUnmanaged}
					<label class="text-muted-foreground flex items-center gap-1.5 font-mono text-[10.5px]">
						<input type="checkbox" bind:checked={showInternal} /> internal topics
					</label>
				{/if}
			</div>

			<div class="overflow-hidden rounded-[4px] border border-border">
				<table class="w-full min-w-[860px] text-left">
					<thead>
						<tr
							class="text-[var(--sb-text-faint)] border-b border-border font-mono text-[10px] uppercase tracking-[0.14em]"
						>
							<th class="px-3 py-2 font-normal">Topic</th>
							<th class="px-3 py-2 font-normal">Partitions</th>
							<th class="px-3 py-2 font-normal">Replication</th>
							<th class="px-3 py-2 font-normal">Source</th>
							<th class="px-3 py-2 font-normal">Landing lag</th>
							<th class="px-3 py-2 font-normal">Retained rows</th>
							<th class="px-3 py-2 font-normal">Retained size</th>
						</tr>
					</thead>
					<tbody>
						{#if loading && !payload}
							<tr><td colspan="7" class="text-muted-foreground px-3 py-6 text-center font-mono text-[11px]">reading broker metadata…</td></tr>
						{:else if visibleTopics.length === 0}
							<tr><td colspan="7" class="text-muted-foreground px-3 py-6 text-center font-mono text-[11px]">no topics match</td></tr>
						{:else}
							{#each visibleTopics as topic (topic.name)}
								<tr class="border-b border-[var(--border-subtle)] last:border-b-0">
									<td class="code px-3 py-1.5 text-[11.5px]">
										{#if topic.sources.length > 0}
											<a
												href="{base}/sources/{topic.sources[0].name}/messages"
												class="text-primary hover:underline"
												title="browse landed messages">{topic.name}</a
											>
										{:else}
											{topic.name}
										{/if}
										{#if topic.internal}<span class="sb-tag ml-1.5">internal</span>{/if}
									</td>
									<td class="text-muted-foreground code px-3 py-1.5 text-[11.5px]"
										>{topic.partitions === null ? '—' : formatInteger(topic.partitions)}</td
									>
									<td class="text-muted-foreground code px-3 py-1.5 text-[11.5px]"
										>{topic.replicationFactor === null ? '—' : topic.replicationFactor}</td
									>
									<td class="px-3 py-1.5">
										{#if topic.sources.length === 0}
											<span class="text-[var(--sb-text-faint)] font-mono text-[10.5px]">not managed</span>
										{:else}
											{#each topic.sources as sourceLink (sourceLink.name)}
												<a
													href="{base}/sources/{sourceLink.name}/messages"
													class="text-primary code mr-2 text-[11.5px] hover:underline"
													title="browse landed messages">{sourceLink.name}</a
												>
											{/each}
										{/if}
									</td>
									<td class="code px-3 py-1.5 text-[11.5px]">
										{#if topic.lagMessages === null}
											<span class="text-[var(--sb-text-faint)]">—</span>
										{:else}
											<span
												style:color={topic.lagMessages > 0
													? 'var(--sb-warning)'
													: 'var(--foreground)'}>{formatCompact(topic.lagMessages)} msg</span
											>
										{/if}
									</td>
									<td class="text-muted-foreground code px-3 py-1.5 text-[11.5px]"
										>{topic.retainedRows === null ? '—' : formatCompact(topic.retainedRows)}</td
									>
									<td class="text-muted-foreground code px-3 py-1.5 text-[11.5px]"
										>{topic.retainedBytes === null ? '—' : formatBytes(topic.retainedBytes)}</td
									>
								</tr>
							{/each}
						{/if}
					</tbody>
				</table>
			</div>

			<p class="text-[var(--sb-text-faint)] pb-4 font-mono text-[10.5px]">
				Landing lag, retained rows, and size come from the warehouse and exist only for managed
				topics. Unmanaged topics are inventory-only.
			</p>
		{/if}
	</div>
</div>
