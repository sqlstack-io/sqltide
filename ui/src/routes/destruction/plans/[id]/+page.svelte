<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import ShieldAlertIcon from '@lucide/svelte/icons/shield-alert';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import Button from '$ui-kit/button/button.svelte';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { destructionOperationLabel } from '$lib/pipeline-view/main/destruction-operation-label';
	import { formatPlanTimestamp } from '$lib/pipeline-view/main/format-plan-timestamp';
	import type { DestructionExecution, DestructionPlan } from '$lib/pipeline-view/types';
	import { createDestructionPlanPageState } from './state.svelte';
	import type { ResourcePageSize } from './types';

	const planId: string = $derived(page.params.id ?? '');
	const destruction = createDestructionPlanPageState();
	const plan: DestructionPlan | null = $derived(
		destruction.requestedId === planId ? destruction.plan : null
	);
	const loadError: string | null = $derived(
		destruction.requestedId === planId ? destruction.error : null
	);
	const title: string = $derived(
		plan === null ? 'Destruction plan' : destructionOperationLabel(plan.operation)
	);

	$effect(() => {
		void destruction.load(planId);
		return destruction.cancel;
	});

	function displayBytes(bytes: number): string {
		return bytes === 0 ? '0 B' : formatBytes(bytes);
	}

	async function execute(): Promise<void> {
		const result: DestructionExecution | null = await destruction.execute();
		if (result !== null) await goto(`${base}/runs/${encodeURIComponent(result.invocationId)}?live=1`);
	}
</script>

<AppTopbar {title} breadcrumb={plan === null ? 'Frozen warehouse impact' : `${plan.target} / ${plan.database}`}>
	<Button variant="outline" size="sm" class="font-mono text-[10.5px]" onclick={() => void goto(`${base}/pipelines`)}>
		<ArrowLeftIcon size={12} /> Pipelines
	</Button>
</AppTopbar>

<div class="min-h-0 flex-1 overflow-y-auto bg-background">
	{#if destruction.loading}
		<div class="grid min-h-[420px] place-items-center px-6 text-center">
			<div>
				<div class="mx-auto mb-3 h-5 w-5 animate-spin rounded-full border-2 border-border border-t-destructive"></div>
				<div class="font-display text-[14px] font-medium">Loading the frozen plan</div>
				<p class="text-muted-foreground mt-1 font-mono text-[10.5px]">No mutation occurs while reading a plan.</p>
			</div>
		</div>
	{:else if plan !== null}
		<div class="grid min-h-full w-full content-start gap-4 p-4 sm:p-5" data-testid="destruction-plan-content">
			<section class="flex items-start gap-3 rounded-md border border-border bg-[var(--sb-surface-low)] p-4">
				<div class="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-destructive/10 text-destructive">
					<ShieldAlertIcon size={17} />
				</div>
				<div>
					<h2 class="font-display text-[14px] font-semibold">Frozen warehouse impact review</h2>
					<p class="text-muted-foreground mt-1 font-mono text-[10.5px]">Refreshing reloads this exact actor-bound plan. It never replans silently.</p>
				</div>
			</section>

			<section class="grid gap-px overflow-hidden rounded-md border border-border bg-border sm:grid-cols-4" aria-label="Plan identity">
				<div class="bg-popover p-3">
					<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.14em]">Operation</div>
					<div class="mt-1 text-[12.5px] font-medium text-destructive">{destructionOperationLabel(plan.operation)}</div>
				</div>
				<div class="bg-popover p-3">
					<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.14em]">Target</div>
					<div class="mt-1 font-mono text-[12px]">{plan.target}</div>
				</div>
				<div class="bg-popover p-3">
					<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.14em]">Database</div>
					<div class="mt-1 truncate font-mono text-[12px]" title={plan.database}>{plan.database}</div>
				</div>
				<div class="bg-popover p-3">
					<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.14em]">Drop safety</div>
					<div class="mt-1 font-mono text-[12px]">
						{!plan.dropSizePolicyObserved ? 'unknown · create a fresh plan' : plan.dropSizeLimitBytes === null ? 'unlimited' : `${displayBytes(plan.dropSizeLimitBytes)} effective`}
					</div>
					{#if plan.dropSizePolicyObserved && plan.dropSizeOverrideBytes !== null}
						<div class="text-[var(--sb-text-faint)] mt-1 font-mono text-[9.5px]">
							{displayBytes(plan.dropSizeOverrideBytes)} SQLTide override · {plan.dropSizeServerLimitBytes === null ? 'server unlimited' : `${displayBytes(plan.dropSizeServerLimitBytes)} server default`}
						</div>
					{/if}
					<div class="text-[var(--sb-text-faint)] mt-1 font-mono text-[9.5px]">{displayBytes(plan.estimatedBytes)} estimated</div>
				</div>
			</section>

			<section aria-labelledby="closure-impact-heading">
				<div class="mb-2 flex items-baseline justify-between gap-3">
					<h3 id="closure-impact-heading" class="font-display text-[13px] font-semibold">Frozen pipeline closure</h3>
					<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">{plan.affectedPipelines.length} affected</span>
				</div>
				<div class="grid gap-2 rounded-md border border-border p-3 sm:grid-cols-2">
					<div>
						<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.13em]">Selected</div>
						<div class="mt-1.5 flex flex-wrap gap-1">
							{#each plan.selectedPipelines as pipeline (pipeline)}
								<code class="rounded bg-muted px-1.5 py-0.5 text-[10.5px]">{pipeline}</code>
							{:else}
								<span class="text-muted-foreground text-[11px]">Entire target</span>
							{/each}
						</div>
					</div>
					<div>
						<div class="text-[var(--sb-text-faint)] font-mono text-[9px] uppercase tracking-[0.13em]">Affected</div>
						<div class="mt-1.5 flex flex-wrap gap-1">
							{#each plan.affectedPipelines as pipeline (pipeline)}
								<code class="rounded bg-destructive/10 px-1.5 py-0.5 text-[10.5px] text-destructive">{pipeline}</code>
							{/each}
						</div>
					</div>
				</div>
			</section>

			<section class="grid items-start gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(270px,0.42fr)]" aria-label="Affected models and retention policy">
				<div class="rounded-md border border-border p-3">
					<div class="flex items-baseline justify-between gap-2">
						<h3 class="font-display text-[13px] font-semibold">Models</h3>
						<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">{plan.models.length}</span>
					</div>
					<div class="mt-2 flex flex-wrap gap-1">
						{#each destruction.modelList.visible as model (model)}
							<code class="rounded bg-muted px-1.5 py-0.5 text-[10.5px]">{model}</code>
						{:else}
							<span class="text-muted-foreground text-[11px]">No models are present in this plan.</span>
						{/each}
					</div>
					{#if plan.models.length > destruction.modelList.visible.length || destruction.modelList.expanded}
						<Button variant="outline" size="sm" class="mt-3 font-mono text-[10.5px]" onclick={() => destruction.modelList.toggle()}>
							{destruction.modelList.expanded ? 'Show fewer models' : `Show all ${plan.models.length} models`}
						</Button>
					{/if}
				</div>
				<div class="rounded-md border border-border p-3">
					<h3 class="font-display text-[13px] font-semibold">Data policy</h3>
					<dl class="mt-2 grid gap-2 text-[11px]">
						<div class="flex items-center justify-between gap-3"><dt class="text-muted-foreground">Managed sources</dt><dd class:!text-destructive={plan.managedSourcesIncluded} class="font-mono">{plan.managedSourcesIncluded ? 'included' : 'preserved'}</dd></div>
						<div class="flex items-center justify-between gap-3"><dt class="text-muted-foreground">Retained replay data</dt><dd class:!text-destructive={plan.retainedReplayDataIncluded} class="font-mono">{plan.retainedReplayDataIncluded ? 'included' : 'preserved'}</dd></div>
					</dl>
				</div>
			</section>

			<section aria-labelledby="resources-heading">
				<div class="mb-2 flex flex-wrap items-baseline justify-between gap-3">
					<h3 id="resources-heading" class="font-display text-[13px] font-semibold">Physical resources</h3>
					<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">
						{plan.resources.length} recorded · {destruction.resourceList.existingCount} existing · {destruction.resourceList.absentCount} absent
					</span>
				</div>
				<div class="overflow-hidden rounded-md border border-border">
					<table class="sb-list w-full table-fixed text-left text-[11px]">
						<caption class="sr-only">Frozen physical resources affected by this destruction plan</caption>
						<thead class="font-mono text-[9px] uppercase tracking-[0.12em] text-[var(--sb-text-faint)]">
							<tr><th class="w-[34%] px-3 py-2 font-normal">Resource</th><th class="w-[14%] px-3 py-2 font-normal">Kind</th><th class="px-3 py-2 font-normal">Logical / pipeline</th><th class="hidden w-[11%] px-3 py-2 text-right font-normal md:table-cell">Bytes</th><th class="hidden w-[8%] px-3 py-2 text-right font-normal lg:table-cell">Parts</th><th class="w-[9%] px-3 py-2 font-normal">State</th></tr>
						</thead>
						<tbody>
							{#each destruction.resourceList.visible as resource (`${resource.kind}:${resource.name}`)}
								<tr data-testid="destruction-resource-row">
									<td class="break-all px-3 py-2 font-mono">{resource.name}</td>
									<td class="break-words px-3 py-2 font-mono text-muted-foreground">{resource.kind.replaceAll('_', ' ')}</td>
									<td class="px-3 py-2"><div class="break-all font-mono">{resource.logicalName}</div><div class="text-[var(--sb-text-faint)] break-all text-[10px]">{resource.pipelineName ?? 'target source'}</div></td>
									<td class="hidden px-3 py-2 text-right font-mono md:table-cell">{resource.bytes === null ? 'unknown' : displayBytes(resource.bytes)}</td>
									<td class="hidden px-3 py-2 text-right font-mono lg:table-cell">{resource.activeParts ?? '—'}</td>
									<td class="px-3 py-2"><span class="sb-tag font-mono" class:text-destructive={resource.exists}>{resource.exists ? 'exists' : 'absent'}</span></td>
								</tr>
							{:else}
								<tr><td colspan="6" class="px-3 py-5 text-center text-muted-foreground">No owned physical resources were found.</td></tr>
							{/each}
						</tbody>
					</table>
					{#if plan.resources.length > 0}
						<div class="flex flex-wrap items-center justify-between gap-3 border-t border-border bg-[var(--sb-surface-low)] px-3 py-2">
							<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">
								Showing {destruction.resourceList.first}-{destruction.resourceList.last} of {plan.resources.length} frozen resources
							</span>
							<div class="flex flex-wrap items-center gap-2">
								<label class="text-muted-foreground flex items-center gap-1.5 font-mono text-[10px]">
									Rows
									<select
										class="h-7 rounded border border-border bg-background px-1.5 text-foreground outline-none focus:border-ring"
										value={destruction.resourceList.pageSize}
										onchange={(event) => destruction.resourceList.setPageSize(Number(event.currentTarget.value) as ResourcePageSize)}
									>
										<option value="25">25</option>
										<option value="50">50</option>
										<option value="100">100</option>
									</select>
								</label>
								<Button
									variant="outline"
									size="sm"
									class="h-7 px-2 font-mono text-[10px]"
									aria-label="Previous resource page"
									disabled={destruction.resourceList.page === 1}
									onclick={() => destruction.resourceList.setPage(destruction.resourceList.page - 1)}
								>
									<ChevronLeftIcon size={12} /> Previous
								</Button>
								<span class="min-w-20 text-center font-mono text-[10px]">
									Page {destruction.resourceList.page} of {destruction.resourceList.pageCount}
								</span>
								<Button
									variant="outline"
									size="sm"
									class="h-7 px-2 font-mono text-[10px]"
									aria-label="Next resource page"
									disabled={destruction.resourceList.page === destruction.resourceList.pageCount}
									onclick={() => destruction.resourceList.setPage(destruction.resourceList.page + 1)}
								>
									Next <ChevronRightIcon size={12} />
								</Button>
							</div>
						</div>
					{/if}
				</div>
			</section>

			<section class="rounded-md border border-destructive/30 bg-destructive/5 p-4" aria-labelledby="irreversible-heading">
				<div class="flex gap-3">
					<ShieldAlertIcon size={16} class="mt-0.5 shrink-0 text-destructive" />
					<div>
						<h3 id="irreversible-heading" class="text-[13px] font-semibold text-destructive">This operation is irreversible</h3>
						<p class="mt-1 text-[11.5px] leading-relaxed">Execution permanently drops the resources listed above from <code>{plan.database}</code>. SQLTide does not provide rollback for destruction operations.</p>
						<p class="mt-2 text-[11.5px] leading-relaxed">Authored project definitions remain. A later build can recreate resources still defined in the project, but it cannot restore their dropped data.</p>
					</div>
				</div>
			</section>

			<section class="rounded-md border border-border bg-[var(--sb-surface-low)] p-4" aria-labelledby="authorization-heading">
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div>
						<h3 id="authorization-heading" class="font-display text-[13px] font-semibold">{destruction.reviewed ? 'Gate 2 · Exact challenges' : 'Gate 1 · Review frozen plan'}</h3>
						<p class="text-muted-foreground mt-1 text-[11px]">Plan <code>{plan.planId}</code> expires {formatPlanTimestamp(plan.expiresAt)}.</p>
					</div>
					<code class="max-w-full break-all text-right text-[9.5px] text-[var(--sb-text-faint)]" title="Plan fingerprint">{plan.planFingerprint}</code>
				</div>

				{#if !destruction.reviewed}
					<p class="mt-3 text-[11.5px]">{plan.dropSizePolicyObserved ? 'Review records that this exact frozen impact was presented. It does not authorize execution.' : 'This legacy plan has no frozen DROP safety evidence and cannot be reviewed. Create a fresh plan.'}</p>
					<Button variant="outline" size="sm" class="mt-3 font-mono text-[11px]" disabled={destruction.reviewing || !plan.dropSizePolicyObserved} onclick={() => void destruction.review()}>
						{destruction.reviewing ? 'Recording review…' : 'Review frozen plan'}
					</Button>
				{:else}
					<p class="mt-3 text-[11.5px]">Type each value exactly as shown. Values are case-sensitive and are sent separately without trimming or normalization. Challenge inputs are cleared on refresh.</p>
					<div class="mt-3 grid gap-3 sm:grid-cols-2">
						{#each plan.challengeValues as challenge, index (`${index}:${challenge}`)}
							<label class="grid gap-1.5" for="destruction-challenge-{index}">
								<span class="text-[10.5px]">Challenge {index + 1}: <code class="select-all text-destructive">{challenge}</code></span>
								<input
									id="destruction-challenge-{index}"
									type="text"
									value={destruction.responses[index] ?? ''}
									autocomplete="off"
									spellcheck="false"
									class="h-9 rounded-md border border-input bg-background px-2.5 font-mono text-[12px] outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
									oninput={(event) => destruction.setResponse(index, event.currentTarget.value)}
								/>
							</label>
						{/each}
					</div>
					<Button variant="destructive" class="mt-4 w-full font-mono text-[11px]" disabled={!destruction.canExecute || destruction.executing} onclick={() => void execute()}>
						{destruction.executing ? 'Executing irreversible operation…' : destructionOperationLabel(plan.operation)}
					</Button>
				{/if}
			</section>

			{#if destruction.error !== null}
				<div class="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2.5 font-mono text-[11px] text-destructive" role="alert">
					{destruction.error}
				</div>
			{/if}
		</div>
	{:else if loadError !== null}
		<div class="grid min-h-[420px] place-items-center px-6 text-center">
			<div class="max-w-xl rounded-md border border-destructive/40 bg-destructive/5 p-5">
				<ShieldAlertIcon size={20} class="mx-auto text-destructive" />
				<h2 class="mt-3 font-display text-[14px] font-semibold">This destruction plan is unavailable</h2>
				<p class="text-muted-foreground mt-2 text-[11.5px]">{loadError}</p>
				<p class="text-muted-foreground mt-2 text-[11px]">It may have expired, already been consumed, or belong to another actor. A replacement plan must be created explicitly.</p>
				<Button variant="outline" size="sm" class="mt-4 font-mono text-[11px]" onclick={() => void goto(`${base}/pipelines`)}>
					Return to pipelines
				</Button>
			</div>
		</div>
	{/if}
</div>
