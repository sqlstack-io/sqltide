<script lang="ts">
	import { base } from '$app/paths';
	import XIcon from '@lucide/svelte/icons/x';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import GraphInspectorContent from '$lib/presentation/components/graph/graph-inspector-content.svelte';
	import StatusPill from '$lib/presentation/components/status-pill.svelte';
	import AnchorBadge from '$lib/presentation/components/anchor-badge.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { modelByName } from '$lib/domain/main/lookups/model-by-name';
	import { sourceByName } from '$lib/domain/main/lookups/source-by-name';
	import { auditsForModel } from '$lib/domain/main/quality/audits-for-model';
	import { testsForModel } from '$lib/domain/main/quality/tests-for-model';
	import {
		type Audit,
		type Model,
		type Project,
		type Source,
		type SqlTest
	} from '$lib/domain/types';
	import type { GraphMode, GraphNode } from '$lib/lineage/types';

	type Props = { node: GraphNode; mode: GraphMode; onclose: () => void };
	let { node, mode, onclose }: Props = $props();

	const project: Project = getProject();
	const model = $derived<Model | undefined>(modelByName(project, node.logicalName));
	const source = $derived<Source | undefined>(sourceByName(project, node.logicalName));
	const audits = $derived<Audit[]>(model ? auditsForModel(project, model.name) : []);
	const tests = $derived<SqlTest[]>(model ? testsForModel(project, model.name) : []);

	type Tab = 'overview' | 'columns' | 'replay' | 'checks';
	let tab = $state<Tab>('overview');

	const detailHref = $derived(
		source ? `${base}/sources/${node.logicalName}` : `${base}/catalog/${node.logicalName}`
	);
</script>

<div class="bg-card flex min-h-full flex-col" data-testid="lineage-inspector">
	<!-- header -->
	<div class="border-b border-border px-4 py-3">
		<div class="flex items-start gap-2">
			<div class="min-w-0 flex-1">
				<div class="truncate font-mono text-[14px] font-medium">{node.label}</div>
				<div class="mt-1 flex flex-wrap items-center gap-2.5">
					<span
						class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.1em]"
						>{node.kindLabel}</span
					>
					<StatusPill status={node.status} />
					{#if node.anchor}<AnchorBadge anchor={node.anchor} />{/if}
				</div>
			</div>
			<a
				href={detailHref}
				class="text-muted-foreground hover:text-foreground flex items-center gap-1 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px]"
			>
				open <ExternalLinkIcon size={11} />
			</a>
			<button
				class="text-muted-foreground hover:text-foreground grid h-[26px] w-[26px] place-items-center rounded-[4px] border border-border"
				aria-label="Close inspector"
				onclick={onclose}
			>
				<XIcon size={13} />
			</button>
		</div>
		{#if mode === 'physical' && node.physicalType}
			<div class="text-[var(--sb-text-faint)] mt-2 font-mono text-[10px]">
				physical object · belongs to <a class="text-primary hover:underline" href={detailHref}
					>{node.logicalName}</a
				>
			</div>
		{/if}
	</div>

	<!-- tabs -->
	<div class="flex shrink-0 gap-1 border-b border-border px-3 py-2">
		{#each [['overview', 'Overview'], ['columns', 'Columns'], ['replay', 'Replay'], ['checks', 'Checks']] as [key, label] (key)}
			<button
				class="rounded-[3px] px-2.5 py-1 font-mono text-[11px] transition-colors {tab === key
					? 'bg-[var(--sb-hover)] text-foreground'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => (tab = key as Tab)}
			>
				{label}
			</button>
		{/each}
	</div>

	<GraphInspectorContent {tab} {project} {model} {source} {audits} {tests} />
</div>
