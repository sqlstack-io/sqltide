<script lang="ts">
	import { base } from '$app/paths';
	import SearchIcon from '@lucide/svelte/icons/search';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import StatusPill from '$lib/presentation/components/status-pill.svelte';
	import AnchorBadge from '$lib/presentation/components/anchor-badge.svelte';
	import { getApp } from '$lib/api/main/project/get-app';
	import { getProject } from '$lib/api/main/project/get-project';
	import { auditCounts } from '$lib/domain/main/quality/audit-counts';
	import { auditsForModel } from '$lib/domain/main/quality/audits-for-model';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import type { Model, Project } from '$lib/domain/types';

	const project: Project = getProject();
	const app = getApp();

	// Catalog is the FLAT index across pipelines — the search surface. Pipelines is
	// where structure is read. Whether both earn a slot at 11 models is an open
	// question recorded in the plan doc.
	type KindFilter = 'all' | 'table' | 'view' | 'aggregate' | 'anchor' | 'drift';

	let query = $state<string>('');
	let kind = $state<KindFilter>('all');

	/**
	 * Which deployment currently backs each logical relation, and how many other
	 * deployments still hold a copy on disk. Only meaningful in virtual mode.
	 */
	const backingByRelation: Map<string, { deploymentId: string; retained: number }> = $derived.by(
		() => {
			const result = new Map<string, { deploymentId: string; retained: number }>();
			for (const deployment of app.deployments) {
				for (const logicalName of deployment.activeBindingNames) {
					result.set(logicalName, { deploymentId: deployment.deploymentId, retained: 0 });
				}
			}
			for (const deployment of app.deployments) {
				for (const relationName of deployment.physicalRelationNames) {
					const parts: string[] = relationName.split('__');
					const logicalName: string = parts.length > 2 ? parts.slice(0, -1).join('__') : relationName;
					const entry: { deploymentId: string; retained: number } | undefined = result.get(logicalName);
					if (entry !== undefined) entry.retained += 1;
				}
			}
			return result;
		}
	);

	function shortDeployment(deploymentId: string): string {
		return deploymentId.split('_').at(-1) ?? deploymentId;
	}

	function matchesKind(model: Model): boolean {
		switch (kind) {
			case 'table':
				return model.kind === 'table' && !model.isAggregate;
			case 'view':
				return model.kind === 'view';
			case 'aggregate':
				return model.isAggregate;
			case 'anchor':
				return model.anchor === 'eligible';
			case 'drift':
				return !model.live.inSyncWithCompiled;
			default:
				return true;
		}
	}

	const filtered = $derived(
		project.models.filter((model) => {
			const needle: string = query.trim().toLowerCase();
			const haystack: string = `${model.name} ${model.relationName} ${model.pipeline} ${model.storage.engine ?? ''}`;
			return matchesKind(model) && (needle === '' || haystack.toLowerCase().includes(needle));
		})
	);

	const chips: { key: KindFilter; label: string }[] = [
		{ key: 'all', label: 'All' },
		{ key: 'table', label: 'Streaming tables' },
		{ key: 'aggregate', label: 'Aggregates' },
		{ key: 'view', label: 'Terminal views' },
		{ key: 'anchor', label: 'Replay anchors' },
		{ key: 'drift', label: 'Drift' }
	];
</script>

<AppTopbar title="Catalog" />

<div class="min-h-0 flex-1 overflow-y-auto">
	<div class="flex flex-wrap items-center gap-2.5 border-b border-border px-[18px] py-2.5">
		<div class="relative">
			<SearchIcon
				size={13}
				class="text-[var(--sb-text-faint)] pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2"
			/>
			<input
				bind:value={query}
				placeholder="Search models and relations…"
				class="bg-[var(--sb-inset)] w-[280px] rounded-[4px] border border-border py-1.5 pl-8 pr-2.5 font-mono text-[11.5px] outline-none focus:border-[var(--primary)]"
			/>
		</div>
		{#each chips as chip (chip.key)}
			<button
				class="rounded-[4px] border px-2.5 py-1.5 font-mono text-[11px] transition-colors {kind ===
				chip.key
					? 'border-primary text-foreground bg-[var(--sidebar-accent)]'
					: 'text-muted-foreground hover:text-foreground border-border'}"
				onclick={() => (kind = chip.key)}
			>
				{chip.label}
			</button>
		{/each}
		<span class="text-[var(--sb-text-faint)] ml-auto font-mono text-[11px]"
			>{filtered.length} of {project.models.length}</span
		>
	</div>

	<table class="sb-list w-full text-left">
		<thead>
			<tr
				class="text-[var(--sb-text-faint)] sticky top-0 z-10 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				<th class="px-[18px] py-2 font-normal">Model</th>
				<th class="px-3 py-2 font-normal">Pipeline</th>
				<th class="px-3 py-2 font-normal">Relation</th>
				<th class="px-3 py-2 font-normal">Backed by</th>
				<th class="px-3 py-2 font-normal">Engine</th>
				<th class="px-3 py-2 font-normal">Replay</th>
				<th class="px-3 py-2 font-normal">Audits</th>
				<th class="px-3 py-2 font-normal">Rows</th>
				<th class="px-3 py-2 font-normal">Disk</th>
				<th class="px-3 py-2 font-normal">Status</th>
				<th class="px-3 py-2 pr-[18px] font-normal">Newest row</th>
			</tr>
		</thead>
		<tbody>
			{#each filtered as model (model.name)}
				{@const counts = auditCounts(auditsForModel(project, model.name))}
				<tr>
					<td class="px-[18px]">
						<a
							href="{base}/catalog/{model.name}"
							class="text-primary code text-[12.5px] font-medium hover:underline">{model.name}</a
						>
					</td>
					<td class="px-3">
						<a
							href="{base}/pipelines/{model.pipeline}"
							class="text-muted-foreground hover:text-foreground text-[12px] hover:underline"
							>{model.pipeline}</a
						>
					</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]">{model.relationName}</td>
					<td class="code px-3 text-[11px]">
						{#if backingByRelation.get(model.relationName)}
							{@const backing = backingByRelation.get(model.relationName)}
							<a
								href="{base}/deployments/{backing?.deploymentId}"
								class="text-primary hover:underline">{shortDeployment(backing?.deploymentId ?? '')}</a
							>
							{#if (backing?.retained ?? 0) > 1}
								<span class="text-[var(--sb-text-faint)]"
									>&nbsp;+{(backing?.retained ?? 1) - 1} retained</span
								>
							{/if}
						{:else}
							<span class="text-[var(--sb-text-faint)]">—</span>
						{/if}
					</td>
					<td class="px-3">
						<span class="sb-tag code">{model.storage.engine ?? 'VIEW'}</span>
					</td>
					<td class="px-3"><AnchorBadge anchor={model.anchor} /></td>
					<td class="px-3">
						{#if counts.total === 0}
							<span class="text-[var(--sb-text-faint)] code text-[11px]">—</span>
						{:else}
							<span
								class="code text-[11.5px]"
								style:color={counts.failing
									? 'var(--sb-error)'
									: counts.warning
										? 'var(--sb-warning)'
										: 'var(--sb-success)'}>{counts.passing}/{counts.total}</span
							>
						{/if}
					</td>
					<td class="text-muted-foreground code px-3 text-[11.5px]"
						>{model.kind === 'view' ? '—' : formatCompact(model.live.rows)}</td
					>
					<td class="text-muted-foreground code px-3 text-[11.5px]"
						>{model.kind === 'view' ? '—' : formatBytes(model.live.diskBytes)}</td
					>
					<td class="px-3"><StatusPill status={model.status} /></td>
					<td class="text-muted-foreground code px-3 pr-[18px] text-[11.5px]"
						>{formatAgo(model.live.newestRowAt, project.capturedAt)}</td
					>
				</tr>
			{/each}
		</tbody>
	</table>

	{#if filtered.length === 0}
		<div class="text-muted-foreground px-[18px] py-8 text-center text-[12.5px]">
			No models match that filter.
		</div>
	{/if}
</div>
