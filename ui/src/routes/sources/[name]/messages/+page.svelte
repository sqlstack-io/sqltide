<script lang="ts">
	import { base } from '$app/paths';
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import { getProject } from '$lib/api/main/project/get-project';
	import { sourceByName } from '$lib/domain/main/lookups/source-by-name';
	import type { Project } from '$lib/domain/types';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import MessageFilters from '$lib/message-browser/components/message-filters.svelte';
	import MessageTable from '$lib/message-browser/components/message-table.svelte';
	import { createMessageBrowserState } from '$lib/message-browser/main/create-message-browser-state.svelte';
	import { encodeFilterDocument } from '$lib/message-browser/main/encode-filter-document';
	import type {
		MessageBrowserState,
		MessageFilterDocument
	} from '$lib/message-browser/types';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import { can } from '$lib/auth/main/can';

	const project: Project = getProject();
	const sourceName = $derived(page.params.name ?? '');
	const source = $derived(sourceByName(project, sourceName));
	const messagesAllowed = $derived(can('source.messages.read'));
	const browser: MessageBrowserState = createMessageBrowserState(page.params.name ?? '');

	function updateDocumentUrl(document: MessageFilterDocument): void {
		const encoded: string | null = encodeFilterDocument(document);
		const url: URL = new URL(page.url);
		if (encoded === null) url.searchParams.delete('q');
		else url.searchParams.set('q', encoded);
		replaceState(url, {});
	}

	$effect(() => () => browser.stop());
</script>

<AppTopbar title="{sourceName} · messages" />

<div class="message-scroller min-h-0 flex-1 overflow-auto">
	{#if !source}
		<div class="px-[18px] py-10 text-center">
			<p class="text-muted-foreground text-[13px]">No source named <code>{sourceName}</code>.</p>
			<a href="{base}/sources" class="text-primary mt-2 inline-block font-mono text-[11.5px]">← Back to sources</a>
		</div>
	{:else if !messagesAllowed}
		<div class="px-[18px] py-10 text-center">
			<p class="text-muted-foreground text-[13px]">
				Browsing raw source messages requires the <code>source.messages.read</code> permission.
			</p>
			<a href="{base}/sources/{sourceName}" class="text-primary mt-2 inline-block font-mono text-[11.5px]"
				>← Back to {sourceName}</a
			>
		</div>
	{:else}
		<div class="flex items-center gap-2.5 border-b border-border px-[18px] py-2.5">
			<a href="{base}/sources/{sourceName}" class="text-muted-foreground hover:text-foreground flex items-center gap-1 font-mono text-[11px]"><ArrowLeftIcon size={12} /> {sourceName}</a>
			{#if source.topic}<span class="sb-tag code">{source.topic}</span>{/if}
			{#if source.live.kafkaLagMessages !== null && source.live.kafkaLagMessages > 0}
				<span class="font-mono text-[10.5px]" style:color="var(--sb-warning)">{formatCompact(source.live.kafkaLagMessages)} messages not yet landed</span>
			{/if}
			<button class="text-muted-foreground hover:text-foreground ml-auto flex items-center gap-1 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px]" onclick={() => void browser.refresh('manual')}><RefreshCwIcon size={11} class={browser.loading ? 'animate-spin' : ''} /> refresh</button>
		</div>

		<div class="flex flex-col gap-3 px-[18px] py-3">
			<MessageFilters
				{browser}
				knownPartitions={source.live.partitions.map((partition) => partition.partition)}
				onDocumentChange={updateDocumentUrl}
			/>
			<MessageTable {browser} {sourceName} />
		</div>
	{/if}
</div>
